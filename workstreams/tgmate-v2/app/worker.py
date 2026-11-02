"""One-shot worker; lease token fences completions. Default adapter never uses network."""
import uuid
from app.db import transaction,unseal,settings
from app.quota import settle

class UnknownSend(Exception): pass
class RetryLater(Exception):
 def __init__(self,seconds): self.seconds=max(1,min(int(seconds),86400))
class PermanentFailure(Exception): pass

class MockTelegram:
 def send(self,target,payload,config): return int(uuid.uuid4().int % 2_000_000_000)+1

class Telegram:
 def __init__(self,client=None): self.client=client
 def send(self,target,payload,config):
  from app.telegram_adapters import BotBroadcast
  result=BotBroadcast(self.client).call(config,'copyMessage',{'chat_id':target,'from_chat_id':payload['source_chat_id'],'message_id':payload['source_message_id']})
  if not isinstance(result,dict) or type(result.get('message_id')) is not int or result['message_id']<=0: raise UnknownSend()
  return result['message_id']

def run_once(tenant,worker_id,adapter=None):
 if adapter is None:
  if settings().get('mode')!='sandbox': raise RuntimeError('Explicit live adapter required outside sandbox')
  adapter=MockTelegram()
 token=uuid.uuid4()
 with transaction(tenant) as db:
  # Expired in-flight sends become uncertain. They are never sent again automatically.
  expired=db.execute("SELECT o.id AS outbox_id,l.operation_id,l.task_id FROM tgmate.outbox_events o JOIN tgmate.relay_runtime_links l ON l.tenant_id=o.tenant_id AND l.outbox_id=o.id WHERE o.status='publishing' AND o.locked_until<now() FOR UPDATE OF o SKIP LOCKED").fetchall()
  for x in expired:
   db.execute("UPDATE tgmate.relay_operations SET status='uncertain' WHERE id=%s AND status='sending'",(x['operation_id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(x['outbox_id'],))
   db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(x['task_id'],))
   db.execute("UPDATE tgmate.task_deliveries SET status='unknown' WHERE task_id=%s AND status='running'",(x['task_id'],))
   db.execute("UPDATE tgmate.delivery_attempts SET status='unknown',finished_at=now() WHERE status='started' AND delivery_id IN (SELECT id FROM tgmate.task_deliveries WHERE task_id=%s)",(x['task_id'],))
   db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'relay.lease_expired','relay_operation',%s)",(tenant,x['operation_id']))
  x=db.execute("SELECT o.id AS outbox_id,o.attempts,l.operation_id,l.task_id,l.payload_id FROM tgmate.outbox_events o JOIN tgmate.relay_runtime_links l ON l.tenant_id=o.tenant_id AND l.outbox_id=o.id WHERE o.status IN ('pending','failed') AND o.available_at<=now() ORDER BY o.created_at FOR UPDATE OF o SKIP LOCKED LIMIT 1").fetchone()
  if not x: return {'status':'idle'}
  op=db.execute('SELECT * FROM tgmate.relay_operations WHERE id=%s FOR UPDATE',(x['operation_id'],)).fetchone()
  c=db.execute('SELECT * FROM tgmate.relay_configs WHERE id=%s FOR UPDATE',(op['config_id'],)).fetchone()
  v=db.execute('SELECT * FROM tgmate.relay_conversations WHERE id=%s FOR UPDATE',(op['conversation_id'],)).fetchone()
  payload=db.execute('SELECT * FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(x['payload_id'],)).fetchone()
  if c['status']!='active' or v['status']!='open' or c['generation']!=op['generation'] or v['generation']!=op['conversation_generation'] or not payload:
   db.execute("UPDATE tgmate.relay_operations SET status='cancelled' WHERE id=%s",(op['id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(x['outbox_id'],))
   db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(x['task_id'],))
   settle(db,tenant,x['task_id'],False)
   return {'status':'cancelled'}
  from app.relay_operator import authorized
  if not authorized(db,c): return {'status':'waiting_for_operator_authorization'}
  if op['direction']=='to_operator':
   from app.relay_privacy import ready
   if not ready(db,tenant,c,v):
    db.execute("UPDATE tgmate.outbox_events SET available_at=now()+interval '2 seconds' WHERE id=%s",(x['outbox_id'],))
    return {'status':'waiting_for_privacy_notice'}
  lease=db.execute("INSERT INTO tgmate.task_leases(tenant_id,task_id,worker_id,lease_token,lease_until) VALUES(%s,%s,%s,%s,now()+interval '60 seconds') ON CONFLICT(tenant_id,task_id) DO UPDATE SET worker_id=EXCLUDED.worker_id,lease_token=EXCLUDED.lease_token,leased_at=now(),lease_until=EXCLUDED.lease_until WHERE tgmate.task_leases.lease_until<now() RETURNING lease_token",(tenant,x['task_id'],worker_id,token)).fetchone()
  if not lease: return {'status':'leased'}
  db.execute("UPDATE tgmate.relay_operations SET status='sending' WHERE id=%s",(op['id'],))
  db.execute("UPDATE tgmate.outbox_events SET status='publishing',locked_by=%s,locked_until=now()+interval '60 seconds',attempts=attempts+1 WHERE id=%s",(worker_id,x['outbox_id']))
  db.execute("UPDATE tgmate.task_registry SET status='running' WHERE id=%s",(x['task_id'],))
  delivery=db.execute("INSERT INTO tgmate.task_deliveries(tenant_id,task_id,destination_key,source_item_key,status) VALUES(%s,%s,%s,%s,'running') ON CONFLICT(tenant_id,task_id,destination_key,source_item_key) DO UPDATE SET status='running' RETURNING id",(tenant,x['task_id'],str(op['target_chat_id']),str(op['id']))).fetchone()['id']
  db.execute("INSERT INTO tgmate.delivery_attempts(tenant_id,delivery_id,attempt_number,status) VALUES(%s,%s,%s,'started')",(tenant,delivery,x['attempts']+1))
  content=unseal(payload['ciphertext'])
 result=None; delay=None
 try:
  result=adapter.send(op['target_chat_id'],content,c)
  if type(result) is not int or result<=0: raise UnknownSend()
  status='succeeded'
 except RetryLater as e:
  status='retry_wait' if x['attempts']<4 else 'failed'
  delay=e.seconds if status=='retry_wait' else None
 except PermanentFailure: status='failed'
 except Exception: status='uncertain'
 with transaction(tenant) as db:
  from app.relay_operator import authorized
  if not authorized(db,c): return {'status':'waiting_for_operator_authorization'}
  if op['direction']=='to_operator':
   from app.relay_privacy import ready
   if not ready(db,tenant,c,v):
    db.execute("UPDATE tgmate.outbox_events SET available_at=now()+interval '2 seconds' WHERE id=%s",(x['outbox_id'],))
    return {'status':'waiting_for_privacy_notice'}
  lease=db.execute('SELECT lease_token,lease_until>now() AS valid FROM tgmate.task_leases WHERE task_id=%s FOR UPDATE',(x['task_id'],)).fetchone()
  if not lease or lease['lease_token']!=token or not lease['valid']: return {'status':'fenced'}
  db.execute('UPDATE tgmate.relay_operations SET status=%s,target_message_id=%s WHERE id=%s',(status,result,op['id']))
  db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s * interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if delay else 'published',delay or 0,x['outbox_id']))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',({'succeeded':'completed','failed':'failed','uncertain':'needs_attention','retry_wait':'queued'}[status],x['task_id']))
  if status in ('succeeded','failed'): settle(db,tenant,x['task_id'],status=='succeeded')
  db.execute('UPDATE tgmate.task_deliveries SET status=%s,remote_message_ref=%s,last_attempt_at=now() WHERE id=%s',({'succeeded':'succeeded','failed':'failed','uncertain':'unknown','retry_wait':'pending'}[status],str(result) if result else None,delivery))
  db.execute('UPDATE tgmate.delivery_attempts SET status=%s,remote_message_ref=%s,finished_at=now() WHERE delivery_id=%s AND attempt_number=%s',({'succeeded':'succeeded','failed':'failed','uncertain':'unknown','retry_wait':'failed'}[status],str(result) if result else None,delivery,x['attempts']+1))
  db.execute('DELETE FROM tgmate.task_leases WHERE task_id=%s AND lease_token=%s',(x['task_id'],token))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,%s,'relay_operation',%s)",(tenant,f'relay.{status}',op['id']))
 return {'status':status}
