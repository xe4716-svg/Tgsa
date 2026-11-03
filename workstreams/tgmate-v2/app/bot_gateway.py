"""One authenticated ingress per Bot, then tenant-scoped routing by actual chat coordinates."""
import hashlib,json
from psycopg.types.json import Jsonb
from .db import transaction
from .relay import Rejected


def route(tenant,bot_id,update):
 if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0: raise Rejected('事件格式错误')
 with transaction(tenant) as db:
  bot=db.execute("SELECT id FROM tgmate.telegram_bots WHERE id=%s AND status='active'",(bot_id,)).fetchone()
  if not bot: raise Rejected('机器人入口不可用')
 query=update.get('callback_query')
 message=update.get('message') or update.get('edited_message')
 if query is not None:
  if not isinstance(query,dict) or not isinstance(query.get('message'),dict): return {'status':'ignored'}
  message=query['message']
 if not isinstance(message,dict) or not isinstance(message.get('chat'),dict): return {'status':'ignored'}
 chat=message['chat']
 if chat.get('type') in ('group','supergroup'):
  with transaction(tenant) as db:
   config=db.execute('SELECT c.id FROM tgmate.moderation_group_configs c JOIN tgmate.telegram_destinations d ON d.tenant_id=c.tenant_id AND d.id=c.destination_id WHERE c.telegram_bot_id=%s AND d.telegram_peer_id=%s',(bot_id,chat.get('id'))).fetchone()
  if not config: return {'status':'ignored_unconfigured_group'}
  if query is not None:
   if not isinstance(query.get('data'),str) or not query['data'].startswith('v:'): return {'status':'ignored'}
   from .verification import callback
   return callback(tenant,config['id'],update)
  from .moderation import receive
  return receive(tenant,config['id'],update)
 if chat.get('type')=='private':
  sender=query.get('from') if query is not None else message.get('from')
  if not isinstance(sender,dict) or sender.get('is_bot') or type(sender.get('id')) is not int or sender['id']<=0 or chat.get('id')!=sender['id']: return {'status':'ignored_private_actor'}
  from .relay_operator import consume
  control=consume(tenant,bot_id,update)
  if control is not None: return control
  from .appeals import receive as appeal
  result=appeal(tenant,bot_id,update)
  if result is not None: return result
  if query is not None: return {'status':'ignored'}
  with transaction(tenant) as db:
   configs=db.execute("SELECT id FROM tgmate.relay_configs WHERE telegram_bot_id=%s AND status='active'",(bot_id,)).fetchall()
  if len(configs)>1: raise Rejected('客服入口存在多个绑定，请管理员指定唯一接待配置')
  if not configs:
   with transaction(tenant) as db:
    paused=db.execute("SELECT id,robot_instance_id FROM tgmate.relay_configs WHERE telegram_bot_id=%s AND status='paused'",(bot_id,)).fetchall()
    if len(paused)==1:
     from .private_replies import enqueue
     enqueue(db,tenant,bot_id,paused[0]['robot_instance_id'],sender['id'],'客服当前暂停接待，请稍后重新发送消息。',f'relay-paused:{bot_id}:{update["update_id"]}',kind='relay_paused')
     return {'status':'paused_notice_queued'}
   return {'status':'ignored_no_private_handler'}
  from .relay import receive
  return receive(tenant,configs[0]['id'],update)
 return {'status':'ignored'}


def dispatch(tenant,bot_id,update):
 if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0: raise Rejected('事件格式错误')
 digest=hashlib.sha256(json.dumps(update,sort_keys=True).encode()).hexdigest()
 source=f'bot:{bot_id}'; event=str(update['update_id'])
 with transaction(tenant) as db:
  db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'bot-ingress:{tenant}:{bot_id}',))
  if not db.execute("SELECT id FROM tgmate.telegram_bots WHERE id=%s AND status='active'",(bot_id,)).fetchone(): raise Rejected('机器人入口不可用')
  old=db.execute('SELECT payload_hash FROM tgmate.inbox_events WHERE source_system=%s AND external_event_id=%s',(source,event)).fetchone()
  if old:
   if old['payload_hash']!=digest: raise Rejected('重复机器人事件内容冲突')
   return {'status':'duplicate'}
  db.execute("INSERT INTO tgmate.inbox_events(tenant_id,source_system,external_event_id,payload_hash,payload,status) VALUES(%s,%s,%s,%s,'{}'::jsonb,'processing')",(tenant,source,event,digest))
  result=route(tenant,bot_id,update)
  db.execute("UPDATE tgmate.inbox_events SET status='processed',payload=%s,processed_at=now() WHERE source_system=%s AND external_event_id=%s",(Jsonb({'result':result}),source,event))
  return result
