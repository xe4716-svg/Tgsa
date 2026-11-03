"""Private Bot reply worker. Payloads expire and unknown sends require human review."""
import uuid
from psycopg.types.json import Jsonb
from .db import transaction,unseal
from .jobs import acquire,valid,release
from .worker import RetryLater,PermanentFailure,UnknownSend


def run_once(tenant,worker_id,adapter):
 token=uuid.uuid4()
 with transaction(tenant) as db:
  expired=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type='bot.private_reply' AND status='publishing' AND locked_until<now() FOR UPDATE SKIP LOCKED").fetchall()
  for job in expired:
   db.execute("UPDATE tgmate.outbox_events SET status='published',payload=payload || %s WHERE id=%s",(Jsonb({'unknown_outcome':True}),job['id']))
   db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(job['payload']['task_id'],))
   db.execute("UPDATE tgmate.task_deliveries SET status='unknown' WHERE task_id=%s AND status='running'",(job['payload']['task_id'],))
   db.execute("UPDATE tgmate.delivery_attempts SET status='unknown',finished_at=now() WHERE status='started' AND delivery_id IN (SELECT id FROM tgmate.task_deliveries WHERE task_id=%s)",(job['payload']['task_id'],))
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type='bot.private_reply' AND status IN ('pending','failed') AND available_at<=now() ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
  if not job: return {'status':'idle'}
  task=job['payload']['task_id']; target=job['payload']['target_user_id']
  if not acquire(db,tenant,task,worker_id,token): return {'status':'leased'}
  bot=db.execute("SELECT * FROM tgmate.telegram_bots WHERE id=%s AND status='active'",(job['aggregate_id'],)).fetchone()
  payload=db.execute('SELECT ciphertext FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(job['payload']['payload_id'],)).fetchone()
  privacy_valid=True
  if job['payload'].get('kind')=='relay_privacy_notice':
   privacy_valid=bool(db.execute("SELECT 1 FROM tgmate.relay_privacy_notices n JOIN tgmate.relay_conversations v ON v.tenant_id=n.tenant_id AND v.id=n.conversation_id JOIN tgmate.relay_configs c ON c.tenant_id=v.tenant_id AND c.id=v.config_id WHERE n.outbox_id=%s AND v.status='open' AND c.status='active' AND c.telegram_bot_id=%s AND v.customer_user_id=%s",(job['id'],job['aggregate_id'],target)).fetchone())
  if not privacy_valid or not bot or not payload or type(target) is not int or target<=0:
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,)); release(db,task,token)
   return {'status':'cancelled'}
  content=unseal(payload['ciphertext'])
  delivery=db.execute("INSERT INTO tgmate.task_deliveries(tenant_id,task_id,destination_key,source_item_key,status) VALUES(%s,%s,%s,%s,'running') ON CONFLICT(tenant_id,task_id,destination_key,source_item_key) DO UPDATE SET status='running' RETURNING id",(tenant,task,f'bot:{bot["id"]}:user:{target}',str(job['id']))).fetchone()['id']
  attempt=db.execute('SELECT coalesce(max(attempt_number),0)+1 AS n FROM tgmate.delivery_attempts WHERE delivery_id=%s',(delivery,)).fetchone()['n']
  db.execute("INSERT INTO tgmate.delivery_attempts(tenant_id,delivery_id,attempt_number,status) VALUES(%s,%s,%s,'started')",(tenant,delivery,attempt))
  db.execute("UPDATE tgmate.outbox_events SET status='publishing',locked_by=%s,locked_until=now()+interval '60 seconds',attempts=attempts+1 WHERE id=%s",(worker_id,job['id']))
  db.execute("UPDATE tgmate.task_registry SET status='running' WHERE id=%s",(task,))
 remote=None; delay=None
 try:
  remote=adapter.send_private(bot,target,content['text'],content.get('markup'))
  if type(remote) is not int or remote<=0: raise UnknownSend()
  status='succeeded'
 except RetryLater as e: status='pending' if attempt<5 else 'failed'; delay=e.seconds if status=='pending' else None
 except PermanentFailure: status='failed'
 except Exception: status='unknown'
 with transaction(tenant) as db:
  if not valid(db,task,token): return {'status':'fenced'}
  db.execute('UPDATE tgmate.task_deliveries SET status=%s,remote_message_ref=%s WHERE id=%s',(status,str(remote) if remote else None,delivery))
  db.execute('UPDATE tgmate.delivery_attempts SET status=%s,remote_message_ref=%s,finished_at=now() WHERE delivery_id=%s AND attempt_number=%s',('failed' if status=='pending' else status,str(remote) if remote else None,delivery,attempt))
  db.execute("UPDATE tgmate.outbox_events SET status=%s,payload=payload || %s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if delay else 'published',Jsonb({'unknown_outcome':status=='unknown'}),delay or 0,job['id']))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',({'succeeded':'completed','failed':'failed','unknown':'needs_attention','pending':'queued'}[status],task))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,%s,'private_reply',%s)",(tenant,f'private_reply.{status}',job['id']))
  release(db,task,token)
 return {'status':status,'reply_id':str(job['id'])}
