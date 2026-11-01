"""Explicit moderation enable/disable with fresh preflight and local change fencing."""
from psycopg.types.json import Jsonb
from app.db import transaction,settings
from app.relay import Rejected


def snapshot(db,identifier,lock=False):
 suffix=' FOR UPDATE OF c' if lock else ''
 row=db.execute('SELECT c.*,b.status AS bot_status,b.telegram_bot_id AS external_bot_id,e.status AS binding_status,e.purpose AS binding_purpose,r.product_type_code,r.deleted_at,d.telegram_peer_id,d.destination_type,d.status AS destination_status,to_jsonb(b) AS bot_snapshot,to_jsonb(e) AS binding_snapshot,to_jsonb(r) AS robot_snapshot,to_jsonb(d) AS destination_snapshot FROM tgmate.moderation_group_configs c JOIN tgmate.telegram_bots b ON b.tenant_id=c.tenant_id AND b.id=c.telegram_bot_id JOIN tgmate.executor_bindings e ON e.tenant_id=c.tenant_id AND e.id=c.executor_binding_id JOIN tgmate.robot_instances r ON r.tenant_id=c.tenant_id AND r.id=c.robot_instance_id JOIN tgmate.telegram_destinations d ON d.tenant_id=c.tenant_id AND d.id=c.destination_id WHERE c.id=%s'+suffix,(identifier,)).fetchone()
 if not row: raise Rejected('未找到群管理配置')
 rules=db.execute('SELECT * FROM tgmate.moderation_rules WHERE group_config_id=%s ORDER BY id'+(' FOR SHARE' if lock else ''),(identifier,)).fetchall()
 return row,rules


def required_permissions(config,rules):
 needed=set()
 if config['cleanup_mode']=='auto_delete': needed.add('can_delete_messages')
 if config['welcome_enabled'] or config['verification_enabled']: needed.add('can_send_messages')
 if config['verification_enabled'] and config['temporary_restrict_on_join']: needed.add('can_restrict_members')
 actions={r['action'] for r in rules if r['enabled']}
 actions.add(config['threshold_action'])
 for action,permission in [('delete','can_delete_messages'),('warn','can_send_messages'),('restrict','can_restrict_members'),('ban','can_ban_members')]:
  if action in actions: needed.add(permission)
 return sorted(needed)


def set_enabled(tenant,identifier,actor,enabled,sandbox=False,adapter=None):
 if type(enabled) is not bool or type(sandbox) is not bool: raise Rejected('启停参数无效')
 with transaction(tenant) as db:
  if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise Rejected('操作者不属于当前工作区')
  if not enabled:
   # Do not touch policy/identity: 004b permits disabling even after binding loss.
   row=db.execute('UPDATE tgmate.moderation_group_configs SET enabled=false,updated_at=now() WHERE id=%s RETURNING id',(identifier,)).fetchone()
   if not row: raise Rejected('未找到群管理配置')
   db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(%s,%s,'moderation.config.disabled','moderation_group_config',%s)",(tenant,actor,identifier))
   return {'config_id':str(identifier),'enabled':False,'verification':'not_required_for_disable'}
  config,rules=snapshot(db,identifier)
 if config['bot_status']!='active' or config['binding_status']!='active' or config['binding_purpose']!='group_manage' or config['product_type_code']!='moderation' or config['deleted_at'] is not None or config['destination_status']!='active' or config['destination_type'] not in ('group','supergroup'): raise Rejected('机器人、执行绑定或群组当前不可用')
 source='injected_test_adapter' if adapter is not None else 'live_adapter'
 if sandbox:
  if settings().get('mode')!='sandbox': raise Rejected('仅本地沙盒允许模拟预检')
  from app.sandbox import SandboxModeration
  adapter=SandboxModeration();source='sandbox_simulated'
 elif adapter is None:
  from app.telegram_adapters import ModerationBot
  adapter=ModerationBot()
 needed=required_permissions(config,rules)
 try: facts=adapter.permissions(config)
 except Exception: raise Rejected('群组权限预检失败，配置仍保持原状态') from None
 if not isinstance(facts,dict) or facts.get('membership_status')!='member' or any(facts.get(p) is not True for p in needed): raise Rejected('机器人不在群内或缺少当前策略所需权限')
 with transaction(tenant) as db:
  if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise Rejected('操作者状态已变更')
  current,current_rules=snapshot(db,identifier,lock=True)
  if current!=config or current_rules!=rules: raise Rejected('配置或执行身份已变更，请重新预检')
  # Simulated permissions must never pollute the real access snapshot.
  if source=='live_adapter':
   db.execute("UPDATE tgmate.bot_destination_access SET membership_status='member',can_send_messages=%s,can_delete_messages=%s,can_restrict_members=%s,can_ban_members=%s,checked_at=now(),last_error_code=NULL,updated_at=now() WHERE telegram_bot_id=%s AND destination_id=%s",tuple(facts.get(k) is True for k in ['can_send_messages','can_delete_messages','can_restrict_members','can_ban_members'])+(config['telegram_bot_id'],config['destination_id']))
  db.execute('UPDATE tgmate.moderation_group_configs SET enabled=true,updated_at=now() WHERE id=%s',(identifier,))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'moderation.config.enabled','moderation_group_config',%s,%s)",(tenant,actor,identifier,Jsonb({'verification':source,'required_permissions':needed})))
 return {'config_id':str(identifier),'enabled':True,'verification':source,'required_permissions':needed}
