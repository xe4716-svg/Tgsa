"""002 immutable text-plan authoring. Execution remains gated behind fresh preflight."""
import hashlib,json,uuid
from datetime import datetime,timezone
from psycopg.types.json import Jsonb
from .db import transaction
from .relay import Rejected

def create_plan(tenant,request_key,module,text,targets,robot=None,binding=None,session=None,scheduled_at=None):
 if module not in ('bot_broadcast','account_broadcast'): raise Rejected('群发通道无效')
 if not isinstance(text,str) or not text.strip() or len(text)>4096: raise Rejected('文本长度无效')
 if not request_key or len(request_key)>128: raise Rejected('幂等键无效')
 targets=sorted(set(map(str,targets)))
 if not targets or len(targets)>100: raise Rejected('请选择1至100个已授权群组')
 if scheduled_at is not None:
  if scheduled_at.tzinfo is None or scheduled_at<=datetime.now(timezone.utc): raise Rejected('计划时间必须为未来的带时区时间')
 recipe={'module':module,'text':text,'targets':targets,'robot':str(robot) if robot else None,'binding':str(binding) if binding else None,'session':str(session) if session else None,'scheduled_at':scheduled_at.isoformat() if scheduled_at else None}
 digest=hashlib.sha256(json.dumps(recipe,sort_keys=True).encode()).hexdigest()
 with transaction(tenant) as db:
  # Transaction-scoped lock serializes same-key creation across concurrent requests.
  db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'{tenant}:{module}:{request_key}',))
  old=db.execute('SELECT id,payload FROM tgmate.task_registry WHERE business_module=%s AND idempotency_key=%s',(module,request_key)).fetchone()
  if old:
   if old['payload'].get('request_hash')!=digest: raise Rejected('同一幂等键不能改变内容')
   return {'task_id':str(old['id']),'duplicate':True}
  for target in targets:
   if not db.execute("SELECT id FROM tgmate.telegram_destinations WHERE id=%s AND status='active'",(target,)).fetchone(): raise Rejected('目标群组无效或不属于当前租户')
  if module=='bot_broadcast':
   executor=db.execute("SELECT e.* FROM tgmate.executor_bindings e JOIN tgmate.robot_instances r ON r.tenant_id=e.tenant_id AND r.id=e.robot_instance_id JOIN tgmate.telegram_bots b ON b.tenant_id=e.tenant_id AND b.id=e.telegram_bot_id WHERE e.id=%s AND e.robot_instance_id=%s AND e.purpose='send' AND e.status='active' AND r.product_type_code='broadcast' AND r.deleted_at IS NULL AND b.status='active'",(binding,robot)).fetchone()
   if not executor or session: raise Rejected('机器人执行绑定无效')
  else:
   if robot or binding: raise Rejected('账号群发是独立通道，不能绑定机器人')
   executor=db.execute("SELECT s.* FROM tgmate.telegram_sessions s JOIN tgmate.telegram_identities i ON i.tenant_id=s.tenant_id AND i.id=s.telegram_identity_id WHERE s.id=%s AND s.authorization_status='authorized' AND i.status='active'",(session,)).fetchone()
   if not executor: raise Rejected('账号会话未授权')
  task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key,payload,scheduled_at) VALUES(%s,%s,%s,'broadcast','draft',%s,%s,%s) RETURNING id",(tenant,module,robot,request_key,Jsonb({'request_hash':digest}),scheduled_at)).fetchone()['id']
  template=db.execute('INSERT INTO tgmate.message_templates(tenant_id,template_key,name) VALUES(%s,%s,%s) RETURNING id',(tenant,str(task),'群发文本')).fetchone()['id']
  version=db.execute("INSERT INTO tgmate.message_template_versions(tenant_id,template_id,version_number,content_type,content_payload,content_sha256,status,published_at) VALUES(%s,%s,1,'text',%s,%s,'published',now()) RETURNING id",(tenant,template,Jsonb({'text':text}),hashlib.sha256(text.encode()).hexdigest())).fetchone()['id']
  db.execute("INSERT INTO tgmate.broadcast_task_versions(tenant_id,task_id,version_number,business_module,template_version_id,schedule_mode,scheduled_at) VALUES(%s,%s,1,%s,%s,%s,%s)",(tenant,task,module,version,'scheduled' if scheduled_at else 'immediate',scheduled_at))
  for target in targets: db.execute('INSERT INTO tgmate.broadcast_task_version_targets(tenant_id,task_id,version_number,destination_id) VALUES(%s,%s,1,%s)',(tenant,task,target))
  if module=='bot_broadcast': db.execute('INSERT INTO tgmate.bot_broadcast_tasks(tenant_id,task_id,robot_instance_id,telegram_bot_id,executor_binding_id) VALUES(%s,%s,%s,%s,%s)',(tenant,task,robot,executor['telegram_bot_id'],binding))
  else:
   db.execute('INSERT INTO tgmate.account_broadcast_tasks(tenant_id,task_id) VALUES(%s,%s)',(tenant,task))
   db.execute('INSERT INTO tgmate.account_broadcast_task_accounts(tenant_id,task_id,telegram_identity_id,telegram_session_id) VALUES(%s,%s,%s,%s)',(tenant,task,executor['telegram_identity_id'],session))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'broadcast.plan.created','task',%s)",(tenant,task))
  return {'task_id':str(task),'duplicate':False}

def preview(tenant,task):
 with transaction(tenant) as db:
  t=db.execute('SELECT id,business_module,status,scheduled_at FROM tgmate.task_registry WHERE id=%s AND business_module IN (\'bot_broadcast\',\'account_broadcast\')',(task,)).fetchone()
  if not t: raise Rejected('未找到群发计划')
  text=db.execute('SELECT m.content_payload FROM tgmate.broadcast_task_versions v JOIN tgmate.message_template_versions m ON m.tenant_id=v.tenant_id AND m.id=v.template_version_id WHERE v.task_id=%s AND v.version_number=1',(task,)).fetchone()
  targets=db.execute('SELECT d.id,d.title,d.destination_type FROM tgmate.broadcast_task_version_targets t JOIN tgmate.telegram_destinations d ON d.tenant_id=t.tenant_id AND d.id=t.destination_id WHERE t.task_id=%s AND t.version_number=1',(task,)).fetchall()
  return {'task':t,'content':text['content_payload'],'targets':targets,'send_ready':False,'reason':'需完成实时权限预检后才能发送'}

# A run is pinned to version 1; future edit/recurrence support creates new snapshots.
def start_run(tenant,task):
 with transaction(tenant) as db:
  t=db.execute("SELECT * FROM tgmate.task_registry WHERE id=%s AND business_module IN ('bot_broadcast','account_broadcast') FOR UPDATE",(task,)).fetchone()
  if not t: raise Rejected('未找到群发计划')
  old=db.execute('SELECT id FROM tgmate.broadcast_task_runs WHERE task_id=%s ORDER BY run_number DESC LIMIT 1',(task,)).fetchone()
  if old: return {'run_id':str(old['id']),'duplicate':True}
  if t['status']!='draft': raise Rejected('当前计划不能启动')
  run=db.execute("INSERT INTO tgmate.broadcast_task_runs(tenant_id,task_id,version_number,business_module,run_number,scheduled_at) VALUES(%s,%s,1,%s,1,%s) RETURNING id",(tenant,task,t['business_module'],t['scheduled_at'])).fetchone()['id']
  targets=db.execute('SELECT destination_id FROM tgmate.broadcast_task_version_targets WHERE task_id=%s AND version_number=1',(task,)).fetchall()
  if t['business_module']=='bot_broadcast':
   executor=db.execute('SELECT * FROM tgmate.bot_broadcast_tasks WHERE task_id=%s',(task,)).fetchone()
   db.execute('INSERT INTO tgmate.bot_broadcast_run_executors(tenant_id,run_id,task_id,telegram_bot_id,executor_binding_id) VALUES(%s,%s,%s,%s,%s)',(tenant,run,task,executor['telegram_bot_id'],executor['executor_binding_id']))
  else:
   executor=db.execute('SELECT * FROM tgmate.account_broadcast_task_accounts WHERE task_id=%s AND is_enabled',(task,)).fetchone()
   if not executor: raise Rejected('未配置独立账号')
   db.execute('INSERT INTO tgmate.account_broadcast_run_accounts(tenant_id,run_id,task_id,telegram_identity_id,telegram_session_id) VALUES(%s,%s,%s,%s,%s)',(tenant,run,task,executor['telegram_identity_id'],executor['telegram_session_id']))
  for target in targets:
   destination=target['destination_id']
   db.execute('INSERT INTO tgmate.broadcast_run_targets(tenant_id,run_id,task_id,version_number,destination_id) VALUES(%s,%s,%s,1,%s)',(tenant,run,task,destination))
   db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload,available_at) VALUES(%s,'broadcast_run',%s,'broadcast.dispatch',%s,%s,coalesce(%s,now()))",(tenant,run,f'broadcast:{run}:{destination}',Jsonb({'destination_id':str(destination),'task_id':str(task)}),t['scheduled_at']))
  db.execute("UPDATE tgmate.task_registry SET status='queued' WHERE id=%s",(task,))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'broadcast.run.queued','broadcast_run',%s)",(tenant,run))
  return {'run_id':str(run),'duplicate':False}

def change_run(tenant,run_id,action):
 if action not in ('pause','resume','cancel'): raise Rejected('操作无效')
 with transaction(tenant) as db:
  run=db.execute('SELECT * FROM tgmate.broadcast_task_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
  if not run: raise Rejected('未找到运行记录')
  if run['status'] in ('completed','failed','cancelled'): raise Rejected('运行已经结束')
  status={'pause':'paused','resume':'queued','cancel':'cancelled'}[action]
  if action=='resume' and run['status']!='paused': raise Rejected('只能恢复已暂停的运行')
  if action=='resume':
   dt='bot_broadcast_deliveries' if run['business_module']=='bot_broadcast' else 'account_broadcast_deliveries'
   if db.execute(f"SELECT id FROM tgmate.{dt} WHERE run_id=%s AND status='unknown' LIMIT 1",(run_id,)).fetchone(): raise Rejected('请先核对所有结果不明的消息')
  db.execute('UPDATE tgmate.broadcast_task_runs SET status=%s WHERE id=%s',(status,run_id))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',(status,run['task_id']))
  if action=='cancel':
   from .quota import settle
   pending=db.execute("SELECT payload FROM tgmate.outbox_events WHERE aggregate_type='broadcast_run' AND aggregate_id=%s AND status IN ('pending','failed') FOR UPDATE",(run_id,)).fetchall()
   for job in pending:
    if job['payload'].get('reservation_id'): settle(db,tenant,run['task_id'],False,job['payload']['reservation_id'])
    if job['payload'].get('delivery_id'):
     dt='bot_broadcast_deliveries' if run['business_module']=='bot_broadcast' else 'account_broadcast_deliveries'
     db.execute(f"UPDATE tgmate.{dt} SET status='skipped' WHERE id=%s AND status='pending'",(job['payload']['delivery_id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE aggregate_type='broadcast_run' AND aggregate_id=%s AND status IN ('pending','failed')",(run_id,))
   db.execute("UPDATE tgmate.broadcast_run_targets SET status='skipped' WHERE run_id=%s AND status IN ('pending','ready')",(run_id,))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,%s,'broadcast_run',%s)",(tenant,f'broadcast.{action}',run_id))
  if action=='resume':
   from .broadcast_worker import complete_run
   complete_run(db,run)
   status=db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s',(run_id,)).fetchone()['status']
  return {'run_id':str(run_id),'status':status,'inflight_may_finish':True}

def review_target(tenant,run_id,destination_id,action,evidence,remote_message_id=None):
 if action not in ('retry','skip','confirm_sent','confirm_failed') or not 10<=len(evidence.strip())<=1000: raise Rejected('请提供有效处理方式和核对证据')
 with transaction(tenant) as db:
  run=db.execute('SELECT * FROM tgmate.broadcast_task_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
  if not run or run['status'] not in ('needs_attention','paused','cancelled'): raise Rejected('请先暂停或处理待审核运行')
  if run['status']=='cancelled' and action not in ('confirm_sent','confirm_failed'): raise Rejected('已取消运行只能核对在途结果')
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE aggregate_type='broadcast_run' AND aggregate_id=%s AND payload->>'destination_id'=%s FOR UPDATE",(run_id,str(destination_id))).fetchone()
  if not job or job['status']=='publishing': raise Rejected('消息仍在发送或目标不存在')
  dt='bot_broadcast_deliveries' if run['business_module']=='bot_broadcast' else 'account_broadcast_deliveries'
  delivery=db.execute(f'SELECT * FROM tgmate.{dt} WHERE id=%s FOR UPDATE',(job['payload'].get('delivery_id'),)).fetchone() if job['payload'].get('delivery_id') else None
  unknown=delivery and delivery['status']=='unknown'
  if unknown and action in ('retry','skip'): raise Rejected('结果不明需先核对送达或失败，不能直接重发或跳过')
  if action in ('confirm_sent','confirm_failed') and not unknown: raise Rejected('只能核对结果不明的消息')
  if action=='confirm_sent' and (type(remote_message_id) is not int or remote_message_id<=0): raise Rejected('需要远端消息编号')
  if delivery and delivery['status']=='succeeded': raise Rejected('已送达消息不能再次处理')
  data=dict(job['payload'])
  from .quota import settle
  if action!='retry' and data.get('reservation_id'): settle(db,tenant,run['task_id'],action=='confirm_sent',data['reservation_id'])
  if action=='retry':
   if data.get('reservation_id'): settle(db,tenant,run['task_id'],False,data['reservation_id']); data.pop('reservation_id')
   if delivery: db.execute(f"UPDATE tgmate.{dt} SET status='pending' WHERE id=%s",(delivery['id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='pending',payload=%s,available_at=now(),locked_until=NULL WHERE id=%s",(Jsonb(data),job['id']))
   target_status='pending'
  else:
   target_status='completed' if action=='confirm_sent' else 'failed' if action=='confirm_failed' else 'skipped'
   if delivery: db.execute(f'UPDATE tgmate.{dt} SET status=%s,remote_message_ref=%s WHERE id=%s',('succeeded' if action=='confirm_sent' else 'failed' if action=='confirm_failed' else 'skipped',str(remote_message_id) if action=='confirm_sent' else None,delivery['id']))
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],))
  db.execute('UPDATE tgmate.broadcast_run_targets SET status=%s,last_error_code=NULL WHERE run_id=%s AND destination_id=%s',(target_status,run_id,destination_id))
  final_status='cancelled' if run['status']=='cancelled' else 'paused'
  db.execute('UPDATE tgmate.broadcast_task_runs SET status=%s WHERE id=%s',(final_status,run_id))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',(final_status,run['task_id']))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id,metadata) VALUES(%s,'broadcast.manual_review','broadcast_run',%s,%s)",(tenant,run_id,Jsonb({'destination_id':str(destination_id),'action':action,'evidence':evidence})))
  return {'run_id':str(run_id),'status':final_status,'target_status':target_status}
