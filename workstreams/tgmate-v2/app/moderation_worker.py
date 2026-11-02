"""003 durable evaluation/action worker. Exemptions and permissions are checked live."""
import uuid
from psycopg.types.json import Jsonb
from app.db import transaction,unseal
from app.jobs import acquire,valid,release
from app.quota import reserve,settle,QuotaExceeded
from app.worker import RetryLater,PermanentFailure,UnknownSend
from app.moderation import evaluate,exempt,prepare_join


def config_for(db,identifier):
 return db.execute('SELECT c.*,d.telegram_peer_id,d.status AS destination_status,b.status AS bot_status,b.telegram_bot_id AS external_bot_id,e.status AS binding_status FROM tgmate.moderation_group_configs c JOIN tgmate.telegram_destinations d ON d.tenant_id=c.tenant_id AND d.id=c.destination_id JOIN tgmate.telegram_bots b ON b.tenant_id=c.tenant_id AND b.id=c.telegram_bot_id JOIN tgmate.executor_bindings e ON e.tenant_id=c.tenant_id AND e.id=c.executor_binding_id WHERE c.id=%s FOR UPDATE OF c',(identifier,)).fetchone()


def active(c): return bool(c and c['enabled'] and c['destination_status']=='active' and c['bot_status']=='active' and c['binding_status']=='active')


def run_once(tenant,worker_id,adapter):
 token=uuid.uuid4()
 with transaction(tenant) as db:
  expired=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type IN ('moderation.evaluate','moderation.action') AND status='publishing' AND locked_until<now() FOR UPDATE SKIP LOCKED").fetchall()
  for job in expired:
   uncertain=job['event_type']=='moderation.action' and job['payload'].get('phase')=='sending'
   if uncertain:
    db.execute("UPDATE tgmate.moderation_actions SET status='manual_review',result=result || %s WHERE id=%s AND status='executing'",(Jsonb({'unknown_outcome':True}),job['aggregate_id']))
    db.execute("UPDATE tgmate.moderation_action_attempts SET status='unknown',finished_at=now() WHERE action_id=%s AND status='started'",(job['aggregate_id'],))
   db.execute('UPDATE tgmate.outbox_events SET status=%s,available_at=now(),locked_until=NULL WHERE id=%s',('published' if uncertain else 'failed',job['id']))
   db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',('needs_attention' if uncertain else 'queued',job['payload']['task_id']))
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type IN ('moderation.evaluate','moderation.action') AND status IN ('pending','failed') AND available_at<=now() ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
  if not job: return {'status':'idle'}
  task=job['payload']['task_id']
  if not acquire(db,tenant,task,worker_id,token): return {'status':'leased'}
  is_eval=job['event_type']=='moderation.evaluate'
  if is_eval:
   event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(job['aggregate_id'],)).fetchone(); action=None
  else:
   action=db.execute('SELECT * FROM tgmate.moderation_actions WHERE id=%s FOR UPDATE',(job['aggregate_id'],)).fetchone()
   event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(action['message_event_id'],)).fetchone()
  c=config_for(db,event['group_config_id'])
  is_cleanup=bool(action and action['source']=='verification' and action['action_type']=='unrestrict')
  def usable(config): return active(config) or bool(is_cleanup and config and config['destination_status']=='active' and config['bot_status']=='active' and config['binding_status']=='active')
  if not usable(c):
   if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,))
   if action:
    db.execute('UPDATE tgmate.moderation_actions SET status=%s WHERE id=%s',('manual_review' if is_cleanup else 'cancelled',action['id']))
    if is_cleanup: db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,))
   release(db,task,token)
   return {'status':'cancelled'}
  db.execute("UPDATE tgmate.outbox_events SET status='publishing',locked_by=%s,locked_until=now()+interval '60 seconds',attempts=attempts+1,payload=payload || %s WHERE id=%s",(worker_id,Jsonb({'phase':'preflight'}),job['id']))
  db.execute("UPDATE tgmate.task_registry SET status='running' WHERE id=%s",(task,))
  payload=db.execute('SELECT ciphertext FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(event['message_snapshot'].get('payload_id'),)).fetchone()
  try: message=unseal(payload['ciphertext']) if payload else None
  except Exception: message=None
  if is_cleanup: message={}
 delay=None; role=None; facts=None
 try:
  role=adapter.member_role(c,event['telegram_user_id'])
  if role not in ('creator','owner','administrator','member','restricted','left','kicked'): raise PermanentFailure()
  if action and action['action_type'] not in ('log_only','request_review'): facts=adapter.permissions(c)
 except RetryLater as e: role=None; delay=e.seconds
 except Exception: role=None
 with transaction(tenant) as db:
  if not valid(db,task,token): return {'status':'fenced'}
  current=config_for(db,c['id'])
  if not usable(current):
   if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
   db.execute("UPDATE tgmate.outbox_events SET status='published' WHERE id=%s",(job['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,))
   if action:
    db.execute('UPDATE tgmate.moderation_actions SET status=%s WHERE id=%s',('manual_review' if is_cleanup else 'cancelled',action['id']))
    if is_cleanup: db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,))
   release(db,task,token); return {'status':'cancelled'}
  if role is None or message is None:
   retry=delay is not None and job['attempts']<4
   db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if retry else 'published',delay or 0,job['id']))
   db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',('queued' if retry else 'needs_attention',task))
   if not retry:
    if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
    if action: db.execute("UPDATE tgmate.moderation_actions SET status='manual_review',permission_check_status='unknown' WHERE id=%s",(action['id'],))
    else: db.execute("UPDATE tgmate.moderation_event_processing SET processing_status='failed',last_error_code='member_check_failed' WHERE message_event_id=%s",(event['id'],))
    db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,review_type) VALUES(%s,%s,%s,'permission_failure') ON CONFLICT DO NOTHING",(tenant,c['id'],event['id']))
   release(db,task,token); return {'status':'retry_wait' if retry else 'manual_review'}
  db.execute('INSERT INTO tgmate.moderation_group_members(tenant_id,group_config_id,telegram_user_id,member_status,last_role_check_at,last_seen_at) VALUES(%s,%s,%s,%s,now(),now()) ON CONFLICT(tenant_id,group_config_id,telegram_user_id) DO UPDATE SET member_status=EXCLUDED.member_status,last_role_check_at=now(),last_seen_at=now()',(tenant,c['id'],event['telegram_user_id'],'owner' if role=='creator' else role))
  if is_eval:
   if event['event_type']=='join': result=prepare_join(db,tenant,event,current)
   else: result=evaluate(db,tenant,event,current,message,role)
   db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='completed',finished_at=now() WHERE id=%s",(task,))
   release(db,task,token); return result
  if exempt(db,c['id'],event['telegram_user_id'],role):
   if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
   db.execute("UPDATE tgmate.moderation_actions SET status='cancelled',reason='exempt_member' WHERE id=%s",(action['id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='cancelled' WHERE id=%s",(task,))
   release(db,task,token); return {'status':'exempt'}
  from app.verification import action_context
  directive,action=action_context(db,action,event,role)
  if directive!='execute':
   if directive=='defer':
    db.execute("UPDATE tgmate.outbox_events SET status='failed',available_at=now()+interval '1 second',locked_until=NULL WHERE id=%s",(job['id'],))
    db.execute("UPDATE tgmate.task_registry SET status='queued' WHERE id=%s",(task,))
   else:
    if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
    db.execute('UPDATE tgmate.moderation_actions SET status=%s WHERE id=%s',('cancelled' if directive=='cancel' else 'manual_review',action['id']))
    db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],))
    db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',('cancelled' if directive=='cancel' else 'needs_attention',task))
   release(db,task,token)
   return {'status':directive}
  kind=action['action_type']
  needed={'delete_message':'can_delete_messages','warn':'can_send_messages','restrict':'can_restrict_members','unrestrict':'can_restrict_members','ban':'can_ban_members','unban':'can_ban_members'}.get(kind)
  allowed=needed is None or isinstance(facts,dict) and facts.get('membership_status')=='member' and facts.get(needed) is True
  if kind=='ban' and not action['requested_by'] and (not current['auto_ban_enabled'] or current['human_review_required_for_ban']): allowed=False
  if not allowed:
   if job['payload'].get('reservation_id'): settle(db,tenant,task,False,job['payload']['reservation_id'])
   db.execute("UPDATE tgmate.moderation_actions SET status='blocked',permission_check_status='denied' WHERE id=%s",(action['id'],))
   db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],))
   db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,))
   db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,violation_id,review_type) VALUES(%s,%s,%s,%s,'permission_failure') ON CONFLICT DO NOTHING",(tenant,c['id'],event['id'],action['violation_id']))
   release(db,task,token); return {'status':'blocked'}
  if facts:
   db.execute('UPDATE tgmate.bot_destination_access SET membership_status=%s,can_send_messages=%s,can_delete_messages=%s,can_restrict_members=%s,can_ban_members=%s,checked_at=now() WHERE telegram_bot_id=%s AND destination_id=%s',('member',facts.get('can_send_messages') is True,facts.get('can_delete_messages') is True,facts.get('can_restrict_members') is True,facts.get('can_ban_members') is True,c['telegram_bot_id'],c['destination_id']))
  attempt=db.execute('SELECT coalesce(max(attempt_number),0)+1 AS n FROM tgmate.moderation_action_attempts WHERE action_id=%s',(action['id'],)).fetchone()['n']
  reservation=job['payload'].get('reservation_id')
  if needed and not is_cleanup:
   try:
    if not reservation: reservation=reserve(db,tenant,task,'moderation.actions',key=f"{action['id']}:{attempt}")
   except QuotaExceeded:
    db.execute("UPDATE tgmate.moderation_actions SET status='blocked',reason='quota_exceeded' WHERE id=%s",(action['id'],))
    db.execute("UPDATE tgmate.outbox_events SET status='published',locked_until=NULL WHERE id=%s",(job['id'],))
    db.execute("UPDATE tgmate.task_registry SET status='needs_attention' WHERE id=%s",(task,))
    release(db,task,token); return {'status':'quota_exceeded'}
  db.execute("INSERT INTO tgmate.moderation_action_attempts(tenant_id,action_id,attempt_number,status,permission_snapshot) VALUES(%s,%s,%s,'started',%s)",(tenant,action['id'],attempt,Jsonb(facts or {})))
  db.execute("UPDATE tgmate.moderation_actions SET status='executing',permission_check_status='allowed' WHERE id=%s",(action['id'],))
  data=dict(job['payload'],phase='sending')
  if reservation: data['reservation_id']=str(reservation)
  db.execute('UPDATE tgmate.outbox_events SET payload=%s WHERE id=%s',(Jsonb(data),job['id']))
 result={}; delay=None
 try:
  if needed:
   result=adapter.execute(current,action,message)
   if not isinstance(result,dict) or result.get('ok') is False: raise UnknownSend()
  status='succeeded'
 except RetryLater as e: status='pending' if attempt<5 else 'failed'; delay=e.seconds if status=='pending' else None
 except PermanentFailure: status='failed'
 except Exception:
  status='manual_review'; result={'unknown_outcome':True}
  if action.get('until_date'): result['planned_until_date']=action['until_date']
 with transaction(tenant) as db:
  if not valid(db,task,token): return {'status':'fenced'}
  db.execute('UPDATE tgmate.moderation_actions SET status=%s,result=%s,executed_at=CASE WHEN %s=\'succeeded\' THEN now() ELSE executed_at END WHERE id=%s',(status,Jsonb(result),status,action['id']))
  db.execute('UPDATE tgmate.moderation_action_attempts SET status=%s,result=%s,finished_at=now() WHERE action_id=%s AND attempt_number=%s',('unknown' if status=='manual_review' else 'failed' if status=='pending' else status,Jsonb(result),action['id'],attempt))
  db.execute("UPDATE tgmate.outbox_events SET status=%s,available_at=now()+(%s*interval '1 second'),locked_until=NULL WHERE id=%s",('failed' if delay else 'published',delay or 0,job['id']))
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',({'succeeded':'completed','failed':'failed','manual_review':'needs_attention','pending':'queued'}[status],task))
  if reservation and status in ('succeeded','failed'): settle(db,tenant,task,status=='succeeded',reservation)
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,%s,'moderation_action',%s)",(tenant,f'moderation.{status}',action['id']))
  release(db,task,token)
 return {'status':status,'action_id':str(action['id'])}
