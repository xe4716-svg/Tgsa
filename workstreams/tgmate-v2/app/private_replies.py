"""Private replies only to users who initiated an authenticated Bot interaction."""
import uuid
from psycopg.types.json import Jsonb
from app.db import seal


def enqueue(db,tenant,bot,robot,user,text,key,kind='appeal_reply',action_id=None,markup=None):
 if type(user) is not int or user<=0 or not isinstance(text,str) or not 1<=len(text)<=4096: raise ValueError('Invalid private response')
 previous=db.execute('SELECT id FROM tgmate.outbox_events WHERE idempotency_key=%s',(key,)).fetchone()
 if previous: return previous['id']
 payload=db.execute("INSERT INTO tgmate.relay_payloads(tenant_id,ciphertext,expires_at) VALUES(%s,%s,now()+interval '1 day') RETURNING id",(tenant,seal({'text':text,'markup':markup}))).fetchone()['id']
 product=db.execute('SELECT product_type_code FROM tgmate.robot_instances WHERE id=%s AND deleted_at IS NULL',(robot,)).fetchone()
 if not product: raise ValueError('Private response robot unavailable')
 module='bot_broadcast' if product['product_type_code']=='broadcast' else product['product_type_code']
 task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,%s,%s,'private_reply','queued',%s) RETURNING id",(tenant,module,robot,key)).fetchone()['id']
 return db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,'telegram_bot',%s,'bot.private_reply',%s,%s) RETURNING id",(tenant,bot,key,Jsonb({'task_id':str(task),'payload_id':str(payload),'target_user_id':user,'kind':kind,'action_id':str(action_id) if action_id else None}))).fetchone()['id']

def review_reply(tenant,identifier,user_id,decision,evidence,remote_message_id=None):
 if decision not in ('confirm_sent','confirm_failed','retry','cancel') or not 10<=len(evidence.strip())<=1000: raise ValueError('需要核对决定和证据')
 from app.db import transaction
 from app.relay import Rejected
 with transaction(tenant) as db:
  if not user_id or not db.execute("SELECT user_id FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(user_id,)).fetchone(): raise Rejected('审核人身份无效')
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE id=%s AND event_type='bot.private_reply' FOR UPDATE",(identifier,)).fetchone()
  if not job or job['status']=='publishing': raise Rejected('私聊回复不存在或仍在发送')
  task=job['payload']['task_id']; state=db.execute('SELECT status FROM tgmate.task_registry WHERE id=%s FOR UPDATE',(task,)).fetchone()['status']
  if state not in ('needs_attention','failed'): raise Rejected('该回复不需要人工处理')
  unknown=job['payload'].get('unknown_outcome') is True
  if unknown and decision in ('retry','cancel'): raise Rejected('结果不明必须先核对，不能直接重发或取消')
  if not unknown and decision.startswith('confirm_'): raise Rejected('该回复没有待核对结果')
  if decision=='confirm_sent' and (type(remote_message_id) is not int or remote_message_id<=0): raise Rejected('需要远端消息编号')
  if decision=='retry':
   if not db.execute('SELECT id FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(job['payload']['payload_id'],)).fetchone(): raise Rejected('回复内容已到保留期限，不能重发')
   db.execute("UPDATE tgmate.outbox_events SET status='pending',payload=payload || %s,available_at=now(),locked_until=NULL WHERE id=%s",(Jsonb({'unknown_outcome':False}),identifier)); outcome='queued'
  else:
   outcome={'confirm_sent':'completed','confirm_failed':'failed','cancel':'cancelled'}[decision]
   db.execute('UPDATE tgmate.outbox_events SET payload=payload || %s WHERE id=%s',(Jsonb({'unknown_outcome':False,'manual_resolution':decision}),identifier))
   db.execute('UPDATE tgmate.task_deliveries SET status=%s,remote_message_ref=%s WHERE task_id=%s',('succeeded' if decision=='confirm_sent' else 'failed' if decision=='confirm_failed' else 'skipped',str(remote_message_id) if decision=='confirm_sent' else None,task))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',(outcome,task))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'private_reply.reviewed','private_reply',%s,%s)",(tenant,user_id,identifier,Jsonb({'decision':decision,'evidence':evidence})))
  return {'reply_id':str(identifier),'status':outcome}
