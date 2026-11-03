"""One durable welcome/challenge/success notice, with conservative unknown-outcome handling."""
import uuid
from psycopg.types.json import Jsonb
from .db import transaction,unseal
from .jobs import acquire,valid,release
from .quota import reserve,settle,QuotaExceeded
from .worker import RetryLater,PermanentFailure,UnknownSend
from .moderation_worker import config_for,active
from .verification import delivered,end_session

TYPES=('moderation.welcome','moderation.challenge','moderation.success')


def review_failure(db,tenant,job,config,event,unknown=False):
 if job['event_type']=='moderation.welcome':
  db.execute("UPDATE tgmate.moderation_welcome_deliveries SET welcome_status='failed',verification_status='manual' WHERE id=%s",(job['aggregate_id'],))
 elif job['event_type']=='moderation.challenge':
  session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s FOR UPDATE',(job['aggregate_id'],)).fetchone()
  if session['status']=='pending': end_session(db,tenant,session,config,'manual_review')
 else:
  welcome_id=job['payload'].get('welcome_id')
  if not welcome_id: welcome_id=db.execute('SELECT challenge_payload FROM tgmate.moderation_verification_sessions WHERE id=%s',(job['aggregate_id'],)).fetchone()['challenge_payload']['welcome_id']
  db.execute("UPDATE tgmate.moderation_welcome_deliveries SET success_message_status='failed' WHERE id=%s",(welcome_id,))
 db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,review_type) VALUES(%s,%s,%s,'verification_manual') ON CONFLICT DO NOTHING",(tenant,config['id'],event['id']))


def fail_preparation(db,tenant,job,config,event,task,token,reservation):
 if reservation: settle(db,tenant,task,False,reservation)
 review_failure(db,tenant,job,config,event)
 db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],))
 db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,))
 release(db,task,token)
 return {'status':'manual_review'}


def run_once(tenant,worker_id,adapter):
 token=uuid.uuid4()
 with transaction(tenant) as db:
  expired=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type=ANY(%s) AND status='publishing' AND locked_until<now() FOR UPDATE SKIP LOCKED",(list(TYPES),)).fetchall()
  for job in expired:
   unknown=job['payload'].get('phase')=='sending'
   if unknown:
    event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(job['payload']['event_id'],)).fetchone(); c=config_for(db,event['group_config_id'])
    review_failure(db,tenant,job,c,event,True)
    db.execute("UPDATE tgmate.task_deliveries SET status='unknown' WHERE task_id=%s AND status='running'",(job['payload']['task_id'],))
    db.execute("UPDATE tgmate.delivery_attempts SET status='unknown',finished_at=now() WHERE status='started' AND delivery_id IN (SELECT id FROM tgmate.task_deliveries WHERE task_id=%s)",(job['payload']['task_id'],))
   db.execute('UPDATE tgmate.outbox_events SET status=%s,payload=payload || %s,available_at=now(),locked_until=NULL WHERE id=%s',('published' if unknown else 'failed',Jsonb({'unknown_outcome':unknown}),job['id']))
   db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',('needs_attention' if unknown else 'queued',job['payload']['task_id']))
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type=ANY(%s) AND status IN ('pending','failed') AND available_at<=now() ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1",(list(TYPES),)).fetchone()
  if not job: return {'status':'idle'}
  task=job['payload']['task_id']
  if not acquire(db,tenant,task,worker_id,token): return {'status':'leased'}
  event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(job['payload']['event_id'],)).fetchone(); c=config_for(db,event['group_config_id'])
  welcome=db.execute('SELECT * FROM tgmate.moderation_welcome_deliveries WHERE id=%s',(job['aggregate_id'],)).fetchone() if job['event_type']=='moderation.welcome' else None
  session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s FOR UPDATE',(job['aggregate_id'],)).fetchone() if not welcome else None
  if not active(c) or session and (job['event_type']=='moderation.challenge' and session['status']!='pending' or job['event_type']=='moderation.success' and session['status']!='passed'):
   if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
   if welcome: db.execute("UPDATE tgmate.moderation_welcome_deliveries SET welcome_status='skipped' WHERE id=%s AND welcome_status='pending'",(welcome['id'],))
   if session and session['status']=='pending': end_session(db,tenant,session,c,'cancelled')
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,)); release(db,task,token)
   return {'status':'cancelled'}
  if job['event_type']=='moderation.challenge':
   restriction=db.execute("SELECT status FROM tgmate.moderation_actions WHERE message_event_id=%s AND source='verification' AND action_type='restrict'",(event['id'],)).fetchone()
   if restriction and restriction['status'] in ('pending','permission_check','queued','executing'):
    db.execute("UPDATE tgmate.outbox_events SET available_at=now()+interval '1 second' WHERE id=%s",(job['id'],)); release(db,task,token); return {'status':'waiting_restriction'}
  if job['event_type']=='moderation.success':
   restore=db.execute("SELECT status FROM tgmate.moderation_actions WHERE message_event_id=%s AND source='verification' AND action_type='unrestrict'",(event['id'],)).fetchone()
   if restore and restore['status'] in ('failed','blocked','manual_review'):
    review_failure(db,tenant,job,c,event)
    db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,)); release(db,task,token)
    return {'status':'manual_review'}
   if restore and restore['status'] not in ('succeeded','cancelled'):
    db.execute("UPDATE tgmate.outbox_events SET available_at=now()+interval '2 seconds' WHERE id=%s",(job['id'],)); release(db,task,token); return {'status':'waiting_restore'}
  data=dict(job['payload'],phase='preflight')
  if session: data['welcome_id']=session['challenge_payload']['welcome_id']
  job['payload']=data
  db.execute("UPDATE tgmate.outbox_events SET status='publishing',locked_by=%s,locked_until=now()+interval '60 seconds',attempts=attempts+1,payload=%s WHERE id=%s",(worker_id,Jsonb(data),job['id']))
  db.execute("UPDATE tgmate.task_registry SET status='running' WHERE id=%s",(task,))
 delay=None; allowed=False
 try:
  facts=adapter.permissions(c); allowed=facts.get('membership_status')=='member' and facts.get('can_send_messages') is True
 except RetryLater as e: delay=e.seconds
 except Exception: pass
 with transaction(tenant) as db:
  if not valid(db,task,token): return {'status':'fenced'}
  current=config_for(db,c['id'])
  if not active(current): allowed=False
  if not allowed:
   retry=delay is not None and job['attempts']<4
   db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if retry else 'published',delay or 0,job['id']))
   db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',('queued' if retry else 'needs_attention',task))
   if not retry:
    if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
    review_failure(db,tenant,job,current,event)
   release(db,task,token); return {'status':'retry_wait' if retry else 'blocked'}
  if welcome and welcome['welcome_status']=='skipped':
   from .verification import start_session
   start_session(db,tenant,welcome,event,current)
   db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='completed' WHERE id=%s",(task,)); release(db,task,token)
   return {'status':'skipped'}
  reservation=job['payload'].get('reservation_id')
  try:
   if not reservation: reservation=reserve(db,tenant,task,'moderation.actions',key=f"{job['id']}:{job['attempts']+1}")
  except QuotaExceeded:
   review_failure(db,tenant,job,current,event)
   db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,)); release(db,task,token)
   return {'status':'quota_exceeded'}
  text=None; markup=None
  if welcome: text=(current['welcome_payload'] or {}).get('text','欢迎加入')
  elif job['event_type']=='moderation.challenge':
   session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s FOR UPDATE',(session['id'],)).fetchone()
   if session['status']!='pending':
    settle(db,tenant,task,False,reservation); db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],)); db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,)); release(db,task,token)
    return {'status':'cancelled'}
   stored=db.execute('SELECT ciphertext FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(session['challenge_payload']['token_payload_id'],)).fetchone()
   if not stored: return fail_preparation(db,tenant,job,current,event,task,token,reservation)
   try: nonce=unseal(stored['ciphertext'])['nonce']
   except Exception: return fail_preparation(db,tenant,job,current,event,task,token,reservation)
   text='请本人点击按钮完成入群验证。'
   markup={'inline_keyboard':[[{'text':'完成验证','callback_data':f"v:{session['id'].hex}:{nonce}"}]]}
  else: text=(current['verification_success_payload'] or {}).get('text','验证成功')
  if not isinstance(text,str) or not 1<=len(text)<=4096: return fail_preparation(db,tenant,job,current,event,task,token,reservation)
  delivery=db.execute("INSERT INTO tgmate.task_deliveries(tenant_id,task_id,destination_key,source_item_key,status) VALUES(%s,%s,%s,%s,'running') ON CONFLICT(tenant_id,task_id,destination_key,source_item_key) DO UPDATE SET status='running' RETURNING id",(tenant,task,str(current['telegram_peer_id']),str(job['id']))).fetchone()['id']
  attempt=db.execute('SELECT coalesce(max(attempt_number),0)+1 AS n FROM tgmate.delivery_attempts WHERE delivery_id=%s',(delivery,)).fetchone()['n']
  db.execute("INSERT INTO tgmate.delivery_attempts(tenant_id,delivery_id,attempt_number,status) VALUES(%s,%s,%s,'started')",(tenant,delivery,attempt))
  data=dict(job['payload'],phase='sending',reservation_id=str(reservation))
  db.execute('UPDATE tgmate.outbox_events SET payload=%s WHERE id=%s',(Jsonb(data),job['id']))
 remote=None; delay=None
 try:
  remote=adapter.send_notice(current,text,markup)
  if type(remote) is not int or remote<=0: raise UnknownSend()
  status='succeeded'
 except RetryLater as e: status='pending' if attempt<5 else 'failed'; delay=e.seconds if status=='pending' else None
 except PermanentFailure: status='failed'
 except Exception: status='unknown'
 with transaction(tenant) as db:
  if not valid(db,task,token): return {'status':'fenced'}
  current=config_for(db,c['id'])
  db.execute('UPDATE tgmate.task_deliveries SET status=%s,remote_message_ref=%s WHERE id=%s',(status,str(remote) if remote else None,delivery))
  db.execute('UPDATE tgmate.delivery_attempts SET status=%s,remote_message_ref=%s,finished_at=now() WHERE delivery_id=%s AND attempt_number=%s',('failed' if status=='pending' else status,str(remote) if remote else None,delivery,attempt))
  db.execute("UPDATE tgmate.outbox_events SET status=%s,payload=payload || %s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if delay else 'published',Jsonb({'unknown_outcome':status=='unknown'}),delay or 0,job['id']))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',({'succeeded':'completed','failed':'failed','unknown':'needs_attention','pending':'queued'}[status],task))
  if status in ('succeeded','failed'): settle(db,tenant,task,status=='succeeded',reservation)
  if status=='succeeded': delivered(db,tenant,job,current,event,remote)
  elif status in ('failed','unknown'): review_failure(db,tenant,job,current,event,status=='unknown')
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,%s,'moderation_notice',%s)",(tenant,f'{job["event_type"]}.{status}',job['aggregate_id']))
  release(db,task,token)
 return {'status':status,'notice':job['event_type']}
