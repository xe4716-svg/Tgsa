"""Best-effort ephemeral callback acknowledgement; never controls verification state."""
import uuid
from psycopg.types.json import Jsonb
from app.db import transaction,unseal
from app.jobs import acquire,valid,release
from app.moderation_worker import config_for
from app.worker import RetryLater,PermanentFailure


def run_once(tenant,worker_id,adapter):
 token=uuid.uuid4()
 with transaction(tenant) as db:
  db.execute("UPDATE tgmate.outbox_events SET status='failed',available_at=now() WHERE event_type='moderation.callback_ack' AND status='publishing' AND locked_until<now()")
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type='moderation.callback_ack' AND status IN ('pending','failed') AND available_at<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
  if not job: return {'status':'idle'}
  task=job['payload']['task_id']
  if not acquire(db,tenant,task,worker_id,token): return {'status':'leased'}
  c=config_for(db,job['payload']['config_id'])
  payload=db.execute('SELECT ciphertext FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(job['payload']['payload_id'],)).fetchone()
  if not payload:
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,)); release(db,task,token)
   return {'status':'expired'}
  content=unseal(payload['ciphertext'])
  db.execute("UPDATE tgmate.outbox_events SET status='publishing',locked_by=%s,locked_until=now()+interval '60 seconds',attempts=attempts+1 WHERE id=%s",(worker_id,job['id']))
 status='completed'; delay=2
 try:
  if adapter.answer_callback(c,content['query_id'],content['text']) is not True: raise ValueError()
 except RetryLater as e: status='queued'; delay=e.seconds
 except PermanentFailure: status='failed'
 except Exception: status='queued'
 if status=='queued' and job['attempts']>=4: status='failed'
 with transaction(tenant) as db:
  if not valid(db,task,token): return {'status':'fenced'}
  db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if status=='queued' else 'published',delay,job['id']))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',(status,task)); release(db,task,token)
 return {'status':status}
