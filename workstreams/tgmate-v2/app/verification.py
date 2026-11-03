"""Durable join verification; callbacks are scoped to user, group, Bot and challenge message."""
import hashlib,secrets,uuid
from datetime import datetime,timezone
from psycopg.types.json import Jsonb
from .db import transaction,seal,unseal
from .relay import Rejected
from .moderation import queue_action


def enqueue(db,tenant,config,kind,aggregate,event_id):
 key=f'{kind}:{aggregate}'
 task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,'moderation',%s,%s,'queued',%s) ON CONFLICT(tenant_id,business_module,idempotency_key) DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING id",(tenant,config['robot_instance_id'],kind,key)).fetchone()['id']
 db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,'moderation_notice',%s,%s,%s,%s) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING",(tenant,aggregate,f'moderation.{kind}',key,Jsonb({'task_id':str(task),'event_id':str(event_id)})))


def cleanup(db,tenant,session,config):
 event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(session['challenge_payload']['event_id'],)).fetchone()
 return queue_action(db,tenant,config,event,'unrestrict','verification',reason=f"verification:{session['id']}")


def end_session(db,tenant,session,config,status):
 db.execute('UPDATE tgmate.moderation_verification_sessions SET status=%s,passed_at=CASE WHEN %s=\'passed\' THEN now() ELSE passed_at END WHERE id=%s',(status,status,session['id']))
 welcome_status='manual' if status in ('manual_review','cancelled') else status
 db.execute('UPDATE tgmate.moderation_welcome_deliveries SET verification_status=%s WHERE id=%s',(welcome_status,session['challenge_payload']['welcome_id']))
 cleanup(db,tenant,session,config)
 if status=='passed' and config['verification_success_payload']:
  db.execute("UPDATE tgmate.moderation_welcome_deliveries SET success_message_status='pending' WHERE id=%s",(session['challenge_payload']['welcome_id'],))
  enqueue(db,tenant,config,'success',session['id'],session['challenge_payload']['event_id'])
 if status in ('expired','failed','manual_review'):
  db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,review_type) VALUES(%s,%s,%s,'verification_manual') ON CONFLICT DO NOTHING",(tenant,config['id'],session['challenge_payload']['event_id']))


def start_session(db,tenant,welcome,event,config):
 if not config['verification_enabled'] or not config['enabled']: return None
 existing=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE group_config_id=%s AND join_event_key=%s',(config['id'],welcome['join_event_key'])).fetchone()
 if existing: return existing
 previous=db.execute("SELECT * FROM tgmate.moderation_verification_sessions WHERE group_config_id=%s AND telegram_user_id=%s AND status='pending' FOR UPDATE",(config['id'],welcome['telegram_user_id'])).fetchone()
 manual=previous is not None
 if previous: end_session(db,tenant,previous,config,'expired')
 nonce=secrets.token_urlsafe(12)
 payload_id=db.execute("INSERT INTO tgmate.relay_payloads(tenant_id,ciphertext,expires_at) VALUES(%s,%s,now()+interval '1 day') RETURNING id",(tenant,seal({'nonce':nonce}))).fetchone()['id']
 data={'event_id':str(event['id']),'welcome_id':str(welcome['id']),'token_hash':hashlib.sha256(nonce.encode()).hexdigest(),'token_payload_id':str(payload_id),'issued':False}
 session=db.execute("INSERT INTO tgmate.moderation_verification_sessions(tenant_id,group_config_id,telegram_user_id,join_event_key,challenge_type,challenge_payload,status,max_attempts,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now()+((%s+120)*interval '1 second')) ON CONFLICT(tenant_id,group_config_id,join_event_key) DO NOTHING RETURNING *",(tenant,config['id'],welcome['telegram_user_id'],welcome['join_event_key'],'admin_review' if manual else 'button',Jsonb(data),'manual_review' if manual else 'pending',config['verification_max_attempts'],config['verification_timeout_seconds'])).fetchone()
 if not session: return None
 db.execute('UPDATE tgmate.moderation_welcome_deliveries SET verification_status=%s WHERE id=%s',('manual' if manual else 'pending',welcome['id']))
 if manual:
  db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,review_type) VALUES(%s,%s,%s,'verification_manual') ON CONFLICT DO NOTHING",(tenant,config['id'],event['id']))
 else:
  if config['temporary_restrict_on_join']: queue_action(db,tenant,config,event,'restrict','verification',reason=f"verification:{session['id']}")
  enqueue(db,tenant,config,'challenge',session['id'],event['id'])
 return session


def delivered(db,tenant,job,config,event,remote):
 if job['event_type']=='moderation.welcome':
  welcome=db.execute("UPDATE tgmate.moderation_welcome_deliveries SET welcome_status='sent',welcome_message_ref=%s,sent_at=now() WHERE id=%s RETURNING *",(str(remote),job['aggregate_id'])).fetchone()
  start_session(db,tenant,welcome,event,config)
 elif job['event_type']=='moderation.challenge':
  session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s FOR UPDATE',(job['aggregate_id'],)).fetchone()
  if session['status']=='pending':
   data=dict(session['challenge_payload'],issued=True,challenge_message_id=remote)
   db.execute('UPDATE tgmate.moderation_verification_sessions SET challenge_payload=%s,expires_at=now()+(%s*interval \'1 second\') WHERE id=%s',(Jsonb(data),config['verification_timeout_seconds'],session['id']))
 else:
  db.execute("UPDATE tgmate.moderation_welcome_deliveries SET success_message_status='sent',success_message_ref=%s WHERE id=%s",(str(remote),job['payload']['welcome_id']))


def callback(tenant,config_id,update):
 if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0: raise Rejected('回调格式错误')
 query=update.get('callback_query',{}); user=query.get('from',{}) if isinstance(query,dict) else {}; message=query.get('message',{}) if isinstance(query,dict) else {}
 if not isinstance(message,dict) or not isinstance(message.get('chat'),dict) or not isinstance(message.get('from'),dict): raise Rejected('回调消息格式错误')
 if not isinstance(user,dict) or user.get('is_bot') or type(user.get('id')) is not int or not isinstance(message,dict): raise Rejected('回调身份错误')
 data=query.get('data','')
 if not isinstance(data,str) or len(data.encode())>64: raise Rejected('回调参数错误')
 try:
  prefix,sid,nonce=data.split(':'); identifier=uuid.UUID(hex=sid)
  if prefix!='v' or not nonce: raise ValueError()
 except (ValueError,TypeError): raise Rejected('验证按钮无效') from None
 with transaction(tenant) as db:
  from .moderation_worker import config_for
  c=config_for(db,config_id)
  if not c: raise Rejected('群组不存在')
  session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s AND group_config_id=%s FOR UPDATE',(identifier,config_id)).fetchone()
  if not session or user['id']!=session['telegram_user_id'] or message.get('chat',{}).get('id')!=c['telegram_peer_id']: raise Rejected('该验证不属于当前用户或群组')
  if not c['external_bot_id'] or message.get('from',{}).get('id')!=c['external_bot_id']: raise Rejected('验证消息来源错误')
  challenge=session['challenge_payload']
  if not challenge.get('issued') or message.get('message_id')!=challenge.get('challenge_message_id'): raise Rejected('验证消息尚未确认或已失效')
  event_key=str(update['update_id']); digest=hashlib.sha256(nonce.encode()).hexdigest()
  event_digest=hashlib.sha256(f"{data}:{user['id']}:{message['message_id']}:{c['telegram_peer_id']}".encode()).hexdigest()
  source=f'verify:{config_id}'
  old=db.execute('SELECT payload_hash FROM tgmate.inbox_events WHERE source_system=%s AND external_event_id=%s',(source,event_key)).fetchone()
  if old:
   if old['payload_hash']!=event_digest: raise Rejected('重复回调冲突')
   enqueue_ack(db,tenant,c,session,query.get('id'),session['status'])
   return {'status':'duplicate'}
  if session['status']!='pending':
   enqueue_ack(db,tenant,c,session,query.get('id'),session['status'])
   return {'status':session['status']}
  if session['challenge_type'] not in ('button',): raise Rejected('该验证需要人工处理')
  expired=session['expires_at']<=datetime.now(timezone.utc)
  passed=not expired and secrets.compare_digest(digest,challenge['token_hash'])
  attempt=session['attempt_count']+1
  result='expired' if expired else 'passed' if passed else 'failed'
  db.execute('INSERT INTO tgmate.moderation_verification_attempts(tenant_id,verification_session_id,attempt_number,response_fingerprint,result) VALUES(%s,%s,%s,%s,%s)',(tenant,session['id'],attempt,digest,result))
  db.execute('UPDATE tgmate.moderation_verification_sessions SET attempt_count=%s WHERE id=%s',(attempt,session['id']))
  if passed or expired or attempt>=session['max_attempts']: end_session(db,tenant,session,c,result)
  db.execute("INSERT INTO tgmate.inbox_events(tenant_id,source_system,external_event_id,payload_hash,payload,status,processed_at) VALUES(%s,%s,%s,%s,%s,'processed',now())",(tenant,source,event_key,event_digest,Jsonb({'session_id':str(session['id'])})))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id,metadata) VALUES(%s,'verification.attempt','verification_session',%s,%s)",(tenant,session['id'],Jsonb({'result':result,'attempt':attempt})))
  enqueue_ack(db,tenant,c,session,query.get('id'),result)
  return {'status':result,'attempt':attempt}


def expire_sessions(tenant):
 count=0
 with transaction(tenant) as db:
  from .moderation_worker import config_for
  candidates=db.execute("SELECT id,group_config_id FROM tgmate.moderation_verification_sessions WHERE status='pending' AND expires_at<=now() LIMIT 100").fetchall()
  for candidate in candidates:
   config=config_for(db,candidate['group_config_id'])
   session=db.execute("SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s AND status='pending' AND expires_at<=now() FOR UPDATE SKIP LOCKED",(candidate['id'],)).fetchone()
   if session: end_session(db,tenant,session,config,'expired'); count+=1
 return count

# Return an execution directive while holding the config/session transaction locks.
def action_context(db,action,event,role):
 reason=action.get('reason') or ''
 if action['source']=='admin' and reason.startswith('appeal:'):
  try: parent_id=uuid.UUID(reason.split(':',1)[1])
  except ValueError: return 'manual',action
  parent=db.execute('SELECT * FROM tgmate.moderation_actions WHERE id=%s AND group_config_id=%s AND telegram_user_id=%s',(parent_id,action['group_config_id'],action['telegram_user_id'])).fetchone()
  if not parent or parent['status']!='succeeded': return 'manual',action
  if action['action_type']=='unrestrict' and parent['action_type']=='restrict':
   until=parent['result'].get('applied_until_date'); fingerprint=parent['result'].get('restriction_fingerprint')
   if type(until) is not int or not fingerprint: return 'manual',action
   return 'execute',dict(action,restore_until_date=until,restore_fingerprint=fingerprint)
  if action['action_type']=='unban' and parent['action_type']=='ban' and parent['result'].get('ban_fingerprint'):
   return 'execute',dict(action,restore_ban_fingerprint=parent['result']['ban_fingerprint'])
  return 'manual',action
 if action['source']!='verification': return 'execute',action
 if not reason.startswith('verification:'): return 'manual',action
 try: identifier=uuid.UUID(reason.split(':',1)[1])
 except ValueError: return 'manual',action
 session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s AND group_config_id=%s AND telegram_user_id=%s FOR UPDATE',(identifier,action['group_config_id'],action['telegram_user_id'])).fetchone()
 if not session: return 'manual',action
 if action['action_type']=='restrict':
  if session['status']!='pending' or session['expires_at']<=datetime.now(timezone.utc): return 'cancel',action
  if role!='member': return 'manual',action
  until=int(datetime.now(timezone.utc).timestamp())+max(120,int((session['expires_at']-datetime.now(timezone.utc)).total_seconds())+60)
  enriched=dict(action,until_date=until)
  db.execute('UPDATE tgmate.moderation_actions SET result=%s WHERE id=%s',(Jsonb({'planned_until_date':until}),action['id']))
  return 'execute',enriched
 if action['action_type']=='unrestrict':
  parent=db.execute("SELECT * FROM tgmate.moderation_actions WHERE message_event_id=%s AND source='verification' AND action_type='restrict'",(event['id'],)).fetchone()
  if not parent or parent['status'] in ('failed','blocked','cancelled'): return 'cancel',action
  if parent['status'] in ('pending','permission_check','queued','executing'): return 'defer',action
  until=parent['result'].get('applied_until_date')
  if parent['status']!='succeeded' or type(until) is not int or until<=0 or not parent['result'].get('restriction_fingerprint'): return 'manual',action
  return 'execute',dict(action,restore_until_date=until,restore_fingerprint=parent['result']['restriction_fingerprint'])
 return 'manual',action

# Explicit human decisions apply to the existing identity-bound session, never to arbitrary users.
def review_session(tenant,identifier,user_id,decision,evidence):
 if decision not in ('approve','reject') or not 10<=len(evidence.strip())<=1000: raise Rejected('请提供人工验证决定和证据')
 with transaction(tenant) as db:
  if not user_id or not db.execute("SELECT user_id FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(user_id,)).fetchone(): raise Rejected('审核人身份无效')
  found=db.execute('SELECT group_config_id FROM tgmate.moderation_verification_sessions WHERE id=%s',(identifier,)).fetchone()
  if not found: raise Rejected('验证会话不存在')
  from .moderation_worker import config_for
  c=config_for(db,found['group_config_id'])
  session=db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
  if session['status'] not in ('pending','manual_review'): raise Rejected('该验证已结束')
  end_session(db,tenant,session,c,'passed' if decision=='approve' else 'failed')
  db.execute("UPDATE tgmate.moderation_review_queue SET status='resolved',assigned_to=%s,decision=%s,decision_note=%s,resolved_at=now() WHERE message_event_id=%s AND review_type='verification_manual' AND status IN ('pending','claimed')",(user_id,decision,evidence,session['challenge_payload']['event_id']))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'verification.manual_review','verification_session',%s,%s)",(tenant,user_id,identifier,Jsonb({'decision':decision,'evidence':evidence})))
  return {'session_id':str(identifier),'status':'passed' if decision=='approve' else 'failed'}


def resolve_notice(tenant,identifier,user_id,decision,evidence,remote_message_id=None):
 if decision not in ('confirm_sent','confirm_failed','retry','cancel') or not 10<=len(evidence.strip())<=1000: raise Rejected('需要投递核对决定及证据')
 with transaction(tenant) as db:
  if not user_id or not db.execute("SELECT user_id FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(user_id,)).fetchone(): raise Rejected('审核人身份无效')
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE id=%s AND event_type IN ('moderation.welcome','moderation.challenge','moderation.success') FOR UPDATE",(identifier,)).fetchone()
  if not job or job['status']=='publishing': raise Rejected('投递不存在或仍在途')
  task=db.execute('SELECT status FROM tgmate.task_registry WHERE id=%s FOR UPDATE',(job['payload']['task_id'],)).fetchone()
  if task['status'] not in ('needs_attention','failed'): raise Rejected('该投递不需要人工核对')
  unknown=job['payload'].get('unknown_outcome') is True
  if unknown and decision in ('retry','cancel'): raise Rejected('结果不明不能直接重发或取消')
  if not unknown and decision.startswith('confirm_'): raise Rejected('该投递没有未决远端结果')
  if decision=='confirm_sent' and (type(remote_message_id) is not int or remote_message_id<=0): raise Rejected('需要远端消息编号')
  event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(job['payload']['event_id'],)).fetchone()
  from .moderation_worker import config_for
  c=config_for(db,event['group_config_id'])
  from .quota import settle
  if job['payload'].get('reservation_id'): settle(db,tenant,job['payload']['task_id'],decision=='confirm_sent',job['payload']['reservation_id'])
  if decision=='retry':
   if job['event_type']=='moderation.challenge':
    session=db.execute('SELECT status FROM tgmate.moderation_verification_sessions WHERE id=%s',(job['aggregate_id'],)).fetchone()
    if session['status']!='pending': raise Rejected('验证已结束，不能复用旧按钮；请人工审核')
   data=dict(job['payload'],phase='preflight',unknown_outcome=False); data.pop('reservation_id',None)
   db.execute("UPDATE tgmate.outbox_events SET status='pending',payload=%s,available_at=now(),locked_until=NULL WHERE id=%s",(Jsonb(data),identifier)); outcome='queued'
  else:
   outcome={'confirm_sent':'completed','confirm_failed':'failed','cancel':'cancelled'}[decision]
   db.execute("UPDATE tgmate.outbox_events SET payload=payload || %s WHERE id=%s",(Jsonb({'unknown_outcome':False,'manual_resolution':decision}),identifier))
   db.execute('UPDATE tgmate.task_deliveries SET status=%s,remote_message_ref=%s WHERE task_id=%s',('succeeded' if decision=='confirm_sent' else 'failed' if decision=='confirm_failed' else 'skipped',str(remote_message_id) if decision=='confirm_sent' else None,job['payload']['task_id']))
   if decision=='confirm_sent': delivered(db,tenant,job,c,event,remote_message_id)
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',(outcome,job['payload']['task_id']))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'verification.notice_review','outbox_event',%s,%s)",(tenant,user_id,identifier,Jsonb({'decision':decision,'evidence':evidence})))
  return {'notice_id':str(identifier),'status':outcome}

# Callback answers are ephemeral UI feedback, independent of verification decisions.
def enqueue_ack(db,tenant,config,session,query_id,result,aggregate_type="verification_session"):
 if not isinstance(query_id,str) or not 1<=len(query_id)<=256: return
 key='callback-ack:'+hashlib.sha256(query_id.encode()).hexdigest()
 if db.execute('SELECT id FROM tgmate.outbox_events WHERE idempotency_key=%s',(key,)).fetchone(): return
 payload=db.execute("INSERT INTO tgmate.relay_payloads(tenant_id,ciphertext,expires_at) VALUES(%s,%s,now()+interval '5 minutes') RETURNING id",(tenant,seal({'query_id':query_id,'text':{'passed':'验证成功','failed':'验证未通过，请重试','expired':'验证已超时，请联系管理员','manual_review':'请等待管理员审核','appeal_prompt':'请回复申诉提示消息填写原因'}.get(result,'该验证已处理')}))).fetchone()['id']
 task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,'moderation',%s,'callback_ack','queued',%s) RETURNING id",(tenant,config['robot_instance_id'],key)).fetchone()['id']
 db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,%s,%s,'moderation.callback_ack',%s,%s)",(tenant,aggregate_type,session['id'],key,Jsonb({'task_id':str(task),'payload_id':str(payload),'config_id':str(config['id'])})))
