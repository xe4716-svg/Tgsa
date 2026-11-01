"""Operator proves private Bot initiation using an expiring identity-bound challenge."""
import hashlib,secrets
from app.db import transaction
from app.relay import Rejected

def authorized(db,config):
 return bool(db.execute('SELECT 1 FROM tgmate.relay_operator_authorizations WHERE config_id=%s AND operator_user_id=%s AND confirmed_at IS NOT NULL',(config['id'],config['operator_user_id'])).fetchone())

def challenge(tenant,config_id,actor):
 code=secrets.token_urlsafe(24);digest=hashlib.sha256(code.encode()).hexdigest()
 with transaction(tenant) as db:
  if not db.execute("SELECT 1 FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(actor,)).fetchone(): raise Rejected('操作者必须是当前工作区成员')
  c=db.execute("SELECT c.*,b.username,b.status AS bot_status FROM tgmate.relay_configs c JOIN tgmate.telegram_bots b ON b.tenant_id=c.tenant_id AND b.id=c.telegram_bot_id WHERE c.id=%s FOR UPDATE OF c",(config_id,)).fetchone()
  if not c or c['status']=='revoked' or c['bot_status']!='active': raise Rejected('客服配置或Bot不可用')
  row=db.execute("INSERT INTO tgmate.relay_operator_authorizations(tenant_id,config_id,operator_user_id,challenge_hash,expires_at,requested_by) VALUES(%s,%s,%s,%s,now()+interval '10 minutes',%s) ON CONFLICT(tenant_id,config_id) DO UPDATE SET operator_user_id=EXCLUDED.operator_user_id,challenge_hash=EXCLUDED.challenge_hash,expires_at=EXCLUDED.expires_at,confirmed_at=NULL,requested_by=EXCLUDED.requested_by,requested_at=now() RETURNING expires_at",(tenant,config_id,c['operator_user_id'],digest,actor)).fetchone()
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(%s,%s,'relay.operator_challenge_created','relay_config',%s)",(tenant,actor,config_id))
  return {'operator_user_id':c['operator_user_id'],'command':'/start relayop_'+code,'expires_at':row['expires_at'],'link':f'https://t.me/{c["username"]}?start=relayop_{code}' if c['username'] else None}

def consume(tenant,bot_id,update):
 if not isinstance(update,dict): return None
 message=update.get('message',{})
 if not isinstance(message,dict): return None
 text=message.get('text')
 if not isinstance(text,str) or not text.startswith('/start relayop_'): return None
 sender=message.get('from',{});chat=message.get('chat',{})
 if not isinstance(sender,dict) or not isinstance(chat,dict) or sender.get('is_bot') or type(sender.get('id')) is not int or sender['id']<=0 or chat.get('type')!='private' or chat.get('id')!=sender['id']: return {'status':'operator_authorization_rejected'}
 code=text[len('/start relayop_'):]
 if len(code)!=32: return {'status':'operator_authorization_rejected'}
 with transaction(tenant) as db:
  row=db.execute("SELECT a.*,c.telegram_bot_id,c.operator_user_id AS current_operator,c.status AS config_status,c.robot_instance_id FROM tgmate.relay_operator_authorizations a JOIN tgmate.relay_configs c ON c.tenant_id=a.tenant_id AND c.id=a.config_id WHERE a.challenge_hash=%s AND c.telegram_bot_id=%s FOR UPDATE OF c,a",(hashlib.sha256(code.encode()).hexdigest(),bot_id)).fetchone()
  if not row or row['config_status']=='revoked' or row['operator_user_id']!=sender['id'] or row['current_operator']!=sender['id']: return {'status':'operator_authorization_rejected'}
  if row['confirmed_at'] is not None: return {'status':'operator_already_authorized'}
  if not db.execute('SELECT %s::timestamptz>now() AS valid',(row['expires_at'],)).fetchone()['valid']: return {'status':'operator_authorization_expired'}
  db.execute('UPDATE tgmate.relay_operator_authorizations SET confirmed_at=now() WHERE config_id=%s',(row['config_id'],))
  from app.private_replies import enqueue
  enqueue(db,tenant,bot_id,row['robot_instance_id'],sender['id'],'客服接收授权已确认。请使用回复功能回应客户中转消息。',f'relay-auth:{row["config_id"]}:{row["challenge_hash"]}',kind='relay_operator_authorized')
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'relay.operator_authorized','relay_config',%s)",(tenant,row['config_id']))
  return {'status':'operator_authorized'}
