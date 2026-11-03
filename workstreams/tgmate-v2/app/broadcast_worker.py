"""Durable one-target broadcast worker; fresh preflight, fenced completion, no rotation."""
import uuid
from psycopg.types.json import Jsonb
from .db import transaction
from .quota import reserve,settle,QuotaExceeded
from .worker import UnknownSend,RetryLater,PermanentFailure


def context(db,run):
 bot=run['business_module']=='bot_broadcast'
 if bot:
  e=db.execute("SELECT x.*,b.status AS credential_status,b.telegram_bot_id AS external_bot_id,e.status AS binding_status,r.deleted_at FROM tgmate.bot_broadcast_run_executors x JOIN tgmate.telegram_bots b ON b.tenant_id=x.tenant_id AND b.id=x.telegram_bot_id JOIN tgmate.executor_bindings e ON e.tenant_id=x.tenant_id AND e.id=x.executor_binding_id JOIN tgmate.robot_instances r ON r.tenant_id=e.tenant_id AND r.id=e.robot_instance_id WHERE x.run_id=%s",(run['id'],)).fetchone()
  valid=e and e['credential_status']=='active' and e['binding_status']=='active' and e['deleted_at'] is None
 else:
  e=db.execute("SELECT x.*,s.authorization_status,i.status AS identity_status,i.telegram_user_id AS external_user_id FROM tgmate.account_broadcast_run_accounts x JOIN tgmate.telegram_sessions s ON s.tenant_id=x.tenant_id AND s.id=x.telegram_session_id JOIN tgmate.telegram_identities i ON i.tenant_id=x.tenant_id AND i.id=x.telegram_identity_id WHERE x.run_id=%s",(run['id'],)).fetchone()
  valid=e and e['authorization_status']=='authorized' and e['identity_status']=='active'
 return bot,e,valid

def tables(bot): return ('bot_broadcast_deliveries','bot_broadcast_delivery_attempts') if bot else ('account_broadcast_deliveries','account_broadcast_delivery_attempts')

def complete_run(db,run):
 states=[r['status'] for r in db.execute('SELECT status FROM tgmate.broadcast_run_targets WHERE run_id=%s',(run['id'],)).fetchall()]
 if any(s in ('pending','ready','running') for s in states): return
 good=states.count('completed')
 status='completed' if good==len(states) else 'partially_completed' if good else 'failed'
 db.execute("UPDATE tgmate.broadcast_task_runs SET status=%s,finished_at=now() WHERE id=%s AND status NOT IN ('cancelled','paused')",(status,run['id']))
 db.execute("UPDATE tgmate.task_registry SET status=%s,finished_at=now() WHERE id=%s AND status NOT IN ('cancelled','paused')",(status,run['task_id']))

def run_once(tenant,worker_id,adapter,*,business_module=None):
 if business_module not in (None,'bot_broadcast','account_broadcast'): raise ValueError('Invalid broadcast module')
 token=uuid.uuid4()
 with transaction(tenant) as db:
  expired=db.execute("SELECT o.* FROM tgmate.outbox_events o JOIN tgmate.broadcast_task_runs r ON r.tenant_id=o.tenant_id AND r.id=o.aggregate_id WHERE o.event_type='broadcast.dispatch' AND o.status='publishing' AND o.locked_until<now() AND (%s::text IS NULL OR r.business_module=%s) FOR UPDATE OF o SKIP LOCKED",(business_module,business_module)).fetchall()
  for old in expired:
   run=db.execute('SELECT * FROM tgmate.broadcast_task_runs WHERE id=%s FOR UPDATE',(old['aggregate_id'],)).fetchone()
   delivery=old['payload'].get('delivery_id')
   if delivery and old['payload'].get('phase')=='sending':
    dt,at=tables(run['business_module']=='bot_broadcast')
    db.execute(f"UPDATE tgmate.{dt} SET status='unknown' WHERE id=%s AND status='running'",(delivery,))
    db.execute(f"UPDATE tgmate.{at} SET status='unknown',finished_at=now() WHERE delivery_id=%s AND status='started'",(delivery,))
    db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(old['id'],))
    db.execute("UPDATE tgmate.broadcast_run_targets SET status='failed',last_error_code='unknown_send' WHERE run_id=%s AND destination_id=%s",(run['id'],old['payload']['destination_id']))
    db.execute("UPDATE tgmate.broadcast_task_runs SET status='needs_attention' WHERE id=%s AND status<>'cancelled'",(run['id'],))
    db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s AND status<>'cancelled'",(run['task_id'],))
   else:
    db.execute("UPDATE tgmate.outbox_events SET status='failed',available_at=now(),locked_until=NULL WHERE id=%s",(old['id'],))
    db.execute("UPDATE tgmate.broadcast_run_targets SET status='pending' WHERE run_id=%s AND destination_id=%s",(run['id'],old['payload']['destination_id']))
  job=db.execute("SELECT o.* FROM tgmate.outbox_events o JOIN tgmate.broadcast_task_runs r ON r.tenant_id=o.tenant_id AND r.id=o.aggregate_id WHERE o.event_type='broadcast.dispatch' AND o.status IN ('pending','failed') AND o.available_at<=now() AND r.status IN ('queued','running') AND (%s::text IS NULL OR r.business_module=%s) ORDER BY o.created_at FOR UPDATE OF r,o SKIP LOCKED LIMIT 1",(business_module,business_module)).fetchone()
  if not job: return {'status':'idle'}
  run=db.execute('SELECT * FROM tgmate.broadcast_task_runs WHERE id=%s FOR UPDATE',(job['aggregate_id'],)).fetchone()
  lease=db.execute("INSERT INTO tgmate.task_leases(tenant_id,task_id,worker_id,lease_token,lease_until) VALUES(%s,%s,%s,%s,now()+interval '60 seconds') ON CONFLICT(tenant_id,task_id) DO UPDATE SET worker_id=EXCLUDED.worker_id,lease_token=EXCLUDED.lease_token,leased_at=now(),lease_until=EXCLUDED.lease_until WHERE tgmate.task_leases.lease_until<now() RETURNING lease_token",(tenant,run['task_id'],worker_id,token)).fetchone()
  if not lease: return {'status':'leased'}
  bot,executor,valid=context(db,run)
  destination=db.execute("SELECT * FROM tgmate.telegram_destinations WHERE id=%s AND status='active'",(job['payload']['destination_id'],)).fetchone()
  db.execute("UPDATE tgmate.outbox_events SET status='publishing',locked_by=%s,locked_until=now()+interval '60 seconds',attempts=attempts+1,payload=payload || %s WHERE id=%s",(worker_id,Jsonb({'phase':'preflight'}),job['id']))
  db.execute("UPDATE tgmate.broadcast_task_runs SET status='running',started_at=coalesce(started_at,now()) WHERE id=%s",(run['id'],))
  db.execute("UPDATE tgmate.task_registry SET status='running',started_at=coalesce(started_at,now()) WHERE id=%s",(run['task_id'],))
  db.execute("UPDATE tgmate.broadcast_run_targets SET status='running' WHERE run_id=%s AND destination_id=%s",(run['id'],job['payload']['destination_id']))
 delay=None; allowed=False
 try:
  if not valid or not destination: raise PermanentFailure()
  facts=adapter.preflight(run['business_module'],executor,destination)
  allowed=facts.get('can_send_messages') is True
 except RetryLater as e: delay=e.seconds
 except Exception: allowed=False
 with transaction(tenant) as db:
  lease=db.execute('SELECT lease_token,lease_until>now() AS valid FROM tgmate.task_leases WHERE task_id=%s FOR UPDATE',(run['task_id'],)).fetchone()
  if not lease or lease['lease_token']!=token or not lease['valid']: return {'status':'fenced'}
  current=db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s FOR UPDATE',(run['id'],)).fetchone()['status']
  if current in ('paused','cancelled'):
   db.execute("UPDATE tgmate.outbox_events SET status=%s,locked_until=NULL WHERE id=%s",('published' if current=='cancelled' else 'pending',job['id']))
   db.execute('UPDATE tgmate.broadcast_run_targets SET status=%s WHERE run_id=%s AND destination_id=%s',('skipped' if current=='cancelled' else 'pending',run['id'],job['payload']['destination_id']))
   db.execute('DELETE FROM tgmate.task_leases WHERE task_id=%s AND lease_token=%s',(run['task_id'],token))
   return {'status':current}
  _,_,still_valid=context(db,run)
  destination_valid=db.execute("SELECT id FROM tgmate.telegram_destinations WHERE id=%s AND status='active'",(job['payload']['destination_id'],)).fetchone()
  if not still_valid or not destination_valid: allowed=False; delay=None
  if not allowed:
   retry=delay is not None and job['attempts']<4
   db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s * interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if retry else 'published',delay or 0,job['id']))
   db.execute("UPDATE tgmate.broadcast_run_targets SET status=%s,last_error_code='preflight_denied',checked_at=now() WHERE run_id=%s AND destination_id=%s",('pending' if retry else 'blocked',run['id'],job['payload']['destination_id']))
   if not retry:
    if job['payload'].get('reservation_id'):
     settle(db,tenant,run['task_id'],False,job['payload']['reservation_id'])
     clean=dict(job['payload']); clean.pop('reservation_id',None)
     db.execute('UPDATE tgmate.outbox_events SET payload=%s WHERE id=%s',(Jsonb(clean),job['id']))
     dt,_=tables(bot)
     db.execute(f"UPDATE tgmate.{dt} SET status='failed',last_error_code='preflight_denied' WHERE id=%s AND status='pending'",(job['payload'].get('delivery_id'),))
    db.execute("UPDATE tgmate.broadcast_task_runs SET status='needs_attention' WHERE id=%s",(run['id'],))
    db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(run['task_id'],))
   db.execute('DELETE FROM tgmate.task_leases WHERE task_id=%s AND lease_token=%s',(run['task_id'],token))
   return {'status':'retry_wait' if retry else 'blocked'}
  # Fresh capability snapshots are persisted before the delivery FK guard is evaluated.
  if bot:
   db.execute("INSERT INTO tgmate.bot_broadcast_run_target_checks(tenant_id,run_id,task_id,telegram_bot_id,destination_id,access_status,can_send_messages,checked_at) VALUES(%s,%s,%s,%s,%s,'allowed',true,now()) ON CONFLICT(tenant_id,run_id,destination_id) DO UPDATE SET access_status='allowed',can_send_messages=true,checked_at=now()",(tenant,run['id'],run['task_id'],executor['telegram_bot_id'],destination['id']))
   dt,at=tables(True)
   delivery=db.execute(f"INSERT INTO tgmate.{dt}(tenant_id,run_id,task_id,telegram_bot_id,destination_id,source_item_key) VALUES(%s,%s,%s,%s,%s,'text:1') ON CONFLICT(tenant_id,run_id,telegram_bot_id,destination_id,source_item_key) DO UPDATE SET status=tgmate.{dt}.status RETURNING *",(tenant,run['id'],run['task_id'],executor['telegram_bot_id'],destination['id'])).fetchone()
  else:
   db.execute("INSERT INTO tgmate.account_broadcast_run_target_checks(tenant_id,run_id,task_id,telegram_identity_id,destination_id,access_status,can_send_messages,checked_at) VALUES(%s,%s,%s,%s,%s,'allowed',true,now()) ON CONFLICT(tenant_id,run_id,telegram_identity_id,destination_id) DO UPDATE SET access_status='allowed',can_send_messages=true,checked_at=now()",(tenant,run['id'],run['task_id'],executor['telegram_identity_id'],destination['id']))
   dt,at=tables(False)
   delivery=db.execute(f"INSERT INTO tgmate.{dt}(tenant_id,run_id,task_id,telegram_identity_id,destination_id,source_item_key) VALUES(%s,%s,%s,%s,%s,'text:1') ON CONFLICT(tenant_id,run_id,telegram_identity_id,destination_id,source_item_key) DO UPDATE SET status=tgmate.{dt}.status RETURNING *",(tenant,run['id'],run['task_id'],executor['telegram_identity_id'],destination['id'])).fetchone()
  reservation=job['payload'].get('reservation_id')
  try:
   if not reservation: reservation=reserve(db,tenant,run['task_id'],'broadcast.messages',key=f"{delivery['id']}:{delivery['attempt_count']+1}")
  except QuotaExceeded:
   db.execute(f"UPDATE tgmate.{dt} SET status='failed',last_error_code='quota_exceeded' WHERE id=%s",(delivery['id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],))
   db.execute("UPDATE tgmate.broadcast_run_targets SET status='blocked',last_error_code='quota_exceeded' WHERE run_id=%s AND destination_id=%s",(run['id'],destination['id']))
   db.execute("UPDATE tgmate.broadcast_task_runs SET status='needs_attention' WHERE id=%s",(run['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(run['task_id'],))
   db.execute('DELETE FROM tgmate.task_leases WHERE task_id=%s AND lease_token=%s',(run['task_id'],token))
   return {'status':'quota_exceeded'}
  data=dict(job['payload'],reservation_id=str(reservation),delivery_id=str(delivery['id']),phase='sending')
  db.execute('UPDATE tgmate.outbox_events SET payload=%s WHERE id=%s',(Jsonb(data),job['id']))
  db.execute(f"UPDATE tgmate.{dt} SET status='running',attempt_count=attempt_count+1 WHERE id=%s",(delivery['id'],))
  attempt=delivery['attempt_count']+1
  db.execute(f"INSERT INTO tgmate.{at}(tenant_id,delivery_id,attempt_number,status) VALUES(%s,%s,%s,'started')",(tenant,delivery['id'],attempt))
  content=db.execute('SELECT m.content_payload FROM tgmate.broadcast_task_versions v JOIN tgmate.message_template_versions m ON m.tenant_id=v.tenant_id AND m.id=v.template_version_id WHERE v.task_id=%s AND v.version_number=1',(run['task_id'],)).fetchone()['content_payload']
 remote=None; delay=None
 try:
  execution=dict(executor,operation_key=str(delivery['id']))
  remote=adapter.send(run['business_module'],execution,destination,content)
  if type(remote) is not int or remote<=0: raise UnknownSend()
  status='succeeded'
 except RetryLater as e: status='pending' if attempt<5 else 'failed'; delay=e.seconds if status=='pending' else None
 except PermanentFailure: status='failed'
 except Exception: status='unknown'
 with transaction(tenant) as db:
  lease=db.execute('SELECT lease_token,lease_until>now() AS valid FROM tgmate.task_leases WHERE task_id=%s FOR UPDATE',(run['task_id'],)).fetchone()
  if not lease or lease['lease_token']!=token or not lease['valid']: return {'status':'fenced'}
  current=db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s FOR UPDATE',(run['id'],)).fetchone()['status']
  if status=='pending' and current=='cancelled': status='skipped'; delay=None
  db.execute(f'UPDATE tgmate.{dt} SET status=%s,remote_message_ref=%s WHERE id=%s',(status,str(remote) if remote else None,delivery['id']))
  db.execute(f'UPDATE tgmate.{at} SET status=%s,remote_message_ref=%s,finished_at=now(),retry_after_seconds=%s WHERE delivery_id=%s AND attempt_number=%s',('failed' if status in ('pending','skipped') else status,str(remote) if remote else None,delay,delivery['id'],attempt))
  if status in ('succeeded','failed','skipped'): settle(db,tenant,run['task_id'],status=='succeeded',reservation)
  db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if delay else 'published',delay or 0,job['id']))
  target_status={'succeeded':'completed','failed':'failed','unknown':'failed','pending':'pending','skipped':'skipped'}[status]
  db.execute('UPDATE tgmate.broadcast_run_targets SET status=%s,last_error_code=%s WHERE run_id=%s AND destination_id=%s',(target_status,'unknown_send' if status=='unknown' else None,run['id'],destination['id']))
  if status in ('failed','unknown') and current!='cancelled':
   db.execute("UPDATE tgmate.broadcast_task_runs SET status='needs_attention' WHERE id=%s",(run['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(run['task_id'],))
  elif status=='succeeded': complete_run(db,run)
  db.execute('DELETE FROM tgmate.task_leases WHERE task_id=%s AND lease_token=%s',(run['task_id'],token))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,%s,'broadcast_run',%s)",(tenant,f'broadcast.{status}',run['id']))
 return {'status':status}
