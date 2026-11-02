"""Durable customer-to-operator relay. Webhook identity is verified by the API."""
import hashlib,json,uuid
from psycopg.types.json import Jsonb
from app.db import transaction,seal
from app.quota import reserve,QuotaExceeded

class Rejected(ValueError): pass

def receive(tenant,config_id,update):
 with transaction(tenant) as db:
  c=db.execute('SELECT * FROM tgmate.relay_configs WHERE id=%s FOR UPDATE',(config_id,)).fetchone()
  if not c or c['status']!='active': raise Rejected('入口未启用')
  from app.relay_operator import consume,authorized
  control=consume(tenant,c['telegram_bot_id'],update)
  if control is not None: return control
  if not authorized(db,c): raise Rejected('客服运营者尚未完成Bot接收授权')
  message=update.get('message')
  if not isinstance(message,dict): return {'status':'ignored'}
  sender=message.get('from',{})
  if not isinstance(sender,dict) or not isinstance(message.get('chat'),dict): raise Rejected('消息格式错误')
  chat=message.get('chat',{})
  if sender.get('is_bot') or chat.get('type')!='private' or chat.get('id')!=sender.get('id'):
   raise Rejected('仅接受真实用户的私聊消息')
  user=sender.get('id'); event=str(update.get('update_id',''))
  if type(user) is not int or user<=0 or type(update.get('update_id')) is not int or update['update_id']<0 or type(message.get('message_id')) is not int or message['message_id']<=0: raise Rejected('消息格式错误')
  if not any(k in message for k in ('text','photo','document','video','audio','voice','sticker','animation')): return {'status':'unsupported'}
  digest=hashlib.sha256(json.dumps(update,sort_keys=True).encode()).hexdigest()
  old=db.execute('SELECT payload_hash FROM tgmate.inbox_events WHERE source_system=%s AND external_event_id=%s',(f'relay:{config_id}',event)).fetchone()
  if old:
   if old['payload_hash']!=digest: raise Rejected('重复事件内容冲突')
   return {'status':'duplicate'}
  recent=db.execute("SELECT count(*) AS n FROM tgmate.inbox_events WHERE source_system=%s AND received_at>now()-interval '1 minute'",(f'relay:{config_id}',)).fetchone()['n']
  queued=db.execute("SELECT count(*) AS n FROM tgmate.relay_operations WHERE config_id=%s AND status IN ('pending','sending','retry_wait')",(config_id,)).fetchone()['n']
  if recent>=120 or queued>=1000: raise Rejected('入口繁忙，请稍后再试')
  per_user=db.execute("SELECT count(*) AS n FROM tgmate.relay_operations WHERE config_id=%s AND source_chat_id=%s AND created_at>now()-interval '1 minute'",(config_id,user)).fetchone()['n']
  if per_user>=20: raise Rejected('发送过快，请稍后再试')
  if user==c['operator_user_id']:
   reply_data=message.get('reply_to_message',{})
   if not isinstance(reply_data,dict): raise Rejected('回复格式错误')
   reply=reply_data.get('message_id')
   if not reply: raise Rejected('请回复客户中转消息')
   parent=db.execute("SELECT o.*,v.customer_user_id,v.status AS conversation_status,v.generation AS current_generation FROM tgmate.relay_operations o JOIN tgmate.relay_conversations v ON v.tenant_id=o.tenant_id AND v.id=o.conversation_id WHERE o.config_id=%s AND o.target_chat_id=%s AND o.target_message_id=%s AND o.direction='to_operator' AND o.status='succeeded'",(config_id,user,reply)).fetchone()
   if not parent or parent['generation']!=c['generation'] or parent['conversation_generation']!=parent['current_generation'] or parent['conversation_status']!='open': raise Rejected('回复映射已失效或客户不可接待')
   v_id=parent['conversation_id']; v_gen=parent['current_generation']; target=parent['customer_user_id']; direction='to_customer'; reply_id=parent['id']
  else:
   # Private contact is the initiation evidence; no two-person pairing invitation.
   v=db.execute("INSERT INTO tgmate.relay_conversations(tenant_id,config_id,customer_user_id,initiated_at,privacy_notice_version) VALUES(%s,%s,%s,now(),'v1') ON CONFLICT(tenant_id,config_id,customer_user_id) DO UPDATE SET customer_user_id=EXCLUDED.customer_user_id RETURNING *",(tenant,config_id,user)).fetchone()
   if v['status']!='open': raise Rejected('会话已暂停或屏蔽')
   from app.relay_privacy import ensure
   ensure(db,tenant,c,v)
   v_id=v['id']; v_gen=v['generation']; target=c['operator_user_id']; direction='to_operator'; reply_id=None
  # No raw update stored in inbox; content is encrypted with a retention deadline.
  db.execute("INSERT INTO tgmate.inbox_events(tenant_id,source_system,external_event_id,payload_hash,payload,status,processed_at) VALUES(%s,%s,%s,%s,%s,'processed',now())",(tenant,f'relay:{config_id}',event,digest,Jsonb({'config_id':str(config_id),'message_id':message['message_id']})))
  payload=db.execute("INSERT INTO tgmate.relay_payloads(tenant_id,ciphertext,expires_at) VALUES(%s,%s,now()+interval '7 days') RETURNING id",(tenant,seal({'source_chat_id':user,'source_message_id':message['message_id']}))).fetchone()['id']
  op=db.execute("INSERT INTO tgmate.relay_operations(tenant_id,config_id,conversation_id,event_key,direction,source_chat_id,source_message_id,target_chat_id,conversation_generation,reply_to_operation_id,payload_vault_ref,generation) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",(tenant,config_id,v_id,event,direction,user,message['message_id'],target,v_gen,reply_id,f'db:{payload}',c['generation'])).fetchone()['id']
  task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,'bidirectional',%s,'relay','queued',%s) RETURNING id",(tenant,c['robot_instance_id'],str(op))).fetchone()['id']
  try: reserve(db,tenant,task)
  except QuotaExceeded as e: raise Rejected(str(e)) from None
  outbox=db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,'relay_operation',%s,'relay.dispatch',%s,%s) RETURNING id",(tenant,op,f'relay:{op}',Jsonb({'operation_id':str(op)}))).fetchone()['id']
  db.execute('INSERT INTO tgmate.relay_runtime_links(tenant_id,operation_id,task_id,outbox_id,payload_id) VALUES(%s,%s,%s,%s,%s)',(tenant,op,task,outbox,payload))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'relay.queued','relay_operation',%s)",(tenant,op))
  return {'status':'queued','operation_id':str(op)}
