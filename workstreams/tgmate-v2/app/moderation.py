"""003 event intake, deterministic rules and transactional violation accounting."""
import hashlib,json,uuid
from datetime import datetime,timezone
import regex
from psycopg.types.json import Jsonb
from app.db import transaction,seal,unseal
from app.relay import Rejected

SUPPORTED_RULES={'keyword','regex','link','invite_link','flood','duplicate','mention_spam'}


def validate_rule(kind,config):
 if kind not in SUPPORTED_RULES or not isinstance(config,dict): raise Rejected('规则类型尚未支持')
 if kind=='keyword':
  words=config.get('words')
  if not isinstance(words,list) or not 1<=len(words)<=100 or any(not isinstance(w,str) or not 1<=len(w)<=200 for w in words): raise Rejected('关键词配置无效')
 if kind=='regex':
  pattern=config.get('pattern')
  if not isinstance(pattern,str) or not 1<=len(pattern)<=500: raise Rejected('表达式配置无效')
  try: regex.compile(pattern)
  except (regex.error,ValueError,OverflowError): raise Rejected('表达式语法错误') from None
 if kind in ('flood','duplicate','mention_spam'):
  if type(config.get('threshold',5)) is not int or not 2<=config.get('threshold',5)<=100: raise Rejected('阈值无效')
  if type(config.get('window_seconds',10)) is not int or not 1<=config.get('window_seconds',10)<=3600: raise Rejected('窗口无效')


def rule_match(rule,message,history):
 kind=rule['rule_type']; config=rule['match_config']; validate_rule(kind,config)
 text=(message.get('text') or message.get('caption') or '')[:16384]
 if not isinstance(text,str): return False
 if kind=='keyword': return any(w.casefold() in text.casefold() for w in config['words'])
 if kind=='regex': return regex.search(config['pattern'],text,timeout=.02) is not None
 entities=message.get('entities',[])+message.get('caption_entities',[])
 if kind=='link': return any(e.get('type') in ('url','text_link') for e in entities) or regex.search(r'https?://|www\.',text,regex.I) is not None
 if kind=='invite_link': return regex.search(r'(?:t\.me|telegram\.me)/(?:joinchat/|\+)',text,regex.I) is not None
 if kind=='mention_spam': return len(regex.findall(r'(?<!\w)@[A-Za-z0-9_]+',text))>=config.get('threshold',5)
 window=config.get('window_seconds',10); threshold=config.get('threshold',5)
 recent=[h for h in history if h['age_seconds']<=window]
 if kind=='flood': return len(recent)>=threshold
 if kind=='duplicate':
  digest=hashlib.sha256(text.encode()).hexdigest()
  return bool(text) and sum(h['content_hash']==digest for h in recent)>=threshold
 return False


def receive(tenant,config_id,update):
 if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0: raise Rejected('事件格式错误')
 kind='edited_message' if 'edited_message' in update else 'message'; message=update.get(kind)
 if not isinstance(message,dict) or not isinstance(message.get('chat'),dict): return {'status':'ignored'}
 chat=message['chat']; sender=message.get('from',{})
 if chat.get('type') not in ('group','supergroup') or type(message.get('message_id')) is not int or message['message_id']<=0: raise Rejected('仅接收已配置群组事件')
 if not isinstance(sender,dict): raise Rejected('消息来源错误')
 if sender.get('is_bot') and not message.get('new_chat_members'): return {'status':'ignored'}
 if not message.get('new_chat_members') and (type(sender.get('id')) is not int or sender['id']<=0):
  # Anonymous administrators cannot safely be punished as a fabricated user.
  return {'status':'ignored_anonymous'}
 text=message.get('text') or message.get('caption') or ''
 if not isinstance(text,str): raise Rejected('文本格式错误')
 if not isinstance(message.get('entities',[]),list) or not isinstance(message.get('caption_entities',[]),list): raise Rejected('实体格式错误')
 if any(not isinstance(e,dict) for e in message.get('entities',[])+message.get('caption_entities',[])): raise Rejected('实体格式错误')
 joins=message.get('new_chat_members',[])
 if not isinstance(joins,list) or any(not isinstance(m,dict) or type(m.get('id')) is not int or m['id']<=0 for m in joins): raise Rejected('入群事件格式错误')
 payload_hash=hashlib.sha256(json.dumps(update,sort_keys=True).encode()).hexdigest()
 with transaction(tenant) as db:
  config=db.execute('SELECT c.*,d.telegram_peer_id FROM tgmate.moderation_group_configs c JOIN tgmate.telegram_destinations d ON d.tenant_id=c.tenant_id AND d.id=c.destination_id WHERE c.id=%s FOR UPDATE OF c',(config_id,)).fetchone()
  if not config or not config['enabled'] or config['telegram_peer_id']!=chat.get('id'): raise Rejected('群组入口不匹配或已停用')
  source=f'moderation:{config_id}'; event=str(update['update_id'])
  old=db.execute('SELECT payload_hash FROM tgmate.inbox_events WHERE source_system=%s AND external_event_id=%s',(source,event)).fetchone()
  if old:
   if old['payload_hash']!=payload_hash: raise Rejected('重复事件内容冲突')
   return {'status':'duplicate'}
  payload=db.execute("INSERT INTO tgmate.relay_payloads(tenant_id,ciphertext,expires_at) VALUES(%s,%s,now()+interval '7 days') RETURNING id",(tenant,seal(message))).fetchone()['id']
  participants=[m['id'] for m in joins if not m.get('is_bot')] if joins else [sender['id']]
  items=[]
  for user in sorted(set(participants)):
   event_key=f'{event}:join:{user}' if joins else event
   item=db.execute('INSERT INTO tgmate.moderation_message_events(tenant_id,group_config_id,external_event_key,telegram_message_id,telegram_user_id,event_type,content_hash,message_snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(tenant,config_id,event_key,message['message_id'],user,'join' if joins else kind,hashlib.sha256(text.encode()).hexdigest(),Jsonb({'payload_id':str(payload)}))).fetchone()['id']
   db.execute('INSERT INTO tgmate.moderation_event_processing(tenant_id,message_event_id) VALUES(%s,%s)',(tenant,item))
   task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,'moderation',%s,'evaluate','queued',%s) RETURNING id",(tenant,config['robot_instance_id'],f'evaluate:{item}')).fetchone()['id']
   db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,'moderation_event',%s,'moderation.evaluate',%s,%s)",(tenant,item,f'evaluate:{item}',Jsonb({'task_id':str(task)})))
   items.append(item)
  db.execute("INSERT INTO tgmate.inbox_events(tenant_id,source_system,external_event_id,payload_hash,payload,status,processed_at) VALUES(%s,%s,%s,%s,%s,'processed',now())",(tenant,source,event,payload_hash,Jsonb({'event_ids':list(map(str,items))})))
  return {'status':'queued' if items else 'ignored','event_id':str(items[0]) if items else None}


def exempt(db,config,user,role):
 if role in ('creator','owner','administrator'): return True
 return bool(db.execute('SELECT id FROM tgmate.moderation_exemptions WHERE group_config_id=%s AND telegram_user_id=%s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())',(config,user)).fetchone())


def queue_action(db,tenant,config,event,kind,source,violation=None,requested_by=None,reason=None):
 key=f"moderation:{event['id']}:{kind}:{source}"
 old=db.execute('SELECT id FROM tgmate.moderation_actions WHERE idempotency_key=%s',(key,)).fetchone()
 if old: return old['id']
 action=db.execute('INSERT INTO tgmate.moderation_actions(tenant_id,group_config_id,message_event_id,violation_id,telegram_user_id,action_type,source,idempotency_key,requested_by,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(tenant,config['id'],event['id'],violation,event['telegram_user_id'],kind,source,key,requested_by,reason)).fetchone()['id']
 task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,'moderation',%s,'action','queued',%s) RETURNING id",(tenant,config['robot_instance_id'],f'action:{action}')).fetchone()['id']
 db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,'moderation_action',%s,'moderation.action',%s,%s)",(tenant,action,f'action:{action}',Jsonb({'task_id':str(task)})))
 return action


def evaluate(db,tenant,event,config,message,member_role):
 existing=db.execute('SELECT processing_status FROM tgmate.moderation_event_processing WHERE message_event_id=%s FOR UPDATE',(event['id'],)).fetchone()
 if existing['processing_status'] in ('processed','ignored'): return {'status':'duplicate'}
 if exempt(db,config['id'],event['telegram_user_id'],member_role):
  db.execute("UPDATE tgmate.moderation_event_processing SET processing_status='ignored',processed_at=now() WHERE message_event_id=%s",(event['id'],))
  return {'status':'exempt'}
 rules=db.execute('SELECT * FROM tgmate.moderation_rules WHERE group_config_id=%s AND enabled ORDER BY priority,id',(config['id'],)).fetchall()
 history=db.execute("SELECT content_hash,extract(epoch FROM %s::timestamptz-received_at) AS age_seconds FROM tgmate.moderation_message_events WHERE group_config_id=%s AND telegram_user_id=%s AND event_type IN ('message','edited_message') AND received_at>%s::timestamptz-interval '1 hour' AND received_at<=%s",(event['received_at'],config['id'],event['telegram_user_id'],event['received_at'],event['received_at'])).fetchall()
 matches=[]; uncertain=False
 for rule in rules:
  try: matched=rule_match(rule,message,history)
  except (Rejected,TimeoutError,regex.error): uncertain=True; continue
  if matched:
   matches.append(rule)
   db.execute('INSERT INTO tgmate.moderation_rule_matches(tenant_id,message_event_id,rule_id,match_details) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,event['id'],rule['id'],Jsonb({'type':rule['rule_type']})))
 violation=None; actions=set(); rule_actions=set()
 if matches:
  counter=db.execute("INSERT INTO tgmate.moderation_violation_counters(tenant_id,group_config_id,telegram_user_id,window_started_at,window_ends_at,violation_count,last_violation_at) VALUES(%s,%s,%s,now(),now()+(%s*interval '1 second'),1,now()) ON CONFLICT(tenant_id,group_config_id,telegram_user_id) DO UPDATE SET window_started_at=CASE WHEN tgmate.moderation_violation_counters.window_ends_at<=now() THEN now() ELSE tgmate.moderation_violation_counters.window_started_at END,window_ends_at=CASE WHEN tgmate.moderation_violation_counters.window_ends_at<=now() THEN now()+(%s*interval '1 second') ELSE tgmate.moderation_violation_counters.window_ends_at END,violation_count=CASE WHEN tgmate.moderation_violation_counters.window_ends_at<=now() THEN 1 ELSE tgmate.moderation_violation_counters.violation_count+1 END,last_violation_at=now() RETURNING *",(tenant,config['id'],event['telegram_user_id'],config['violation_window_seconds'],config['violation_window_seconds'])).fetchone()
  severity=max(matches,key=lambda r:['low','medium','high','critical'].index(r['severity']))['severity']
  violation=db.execute('INSERT INTO tgmate.moderation_violations(tenant_id,group_config_id,message_event_id,telegram_user_id,severity,summary,counted_in_window,window_started_at,window_ends_at) VALUES(%s,%s,%s,%s,%s,%s,true,%s,%s) RETURNING id',(tenant,config['id'],event['id'],event['telegram_user_id'],severity,'命中群管理规则',counter['window_started_at'],counter['window_ends_at'])).fetchone()['id']
  for rule in matches: actions.add(rule['action'])
  rule_actions=set(actions)
  if counter['violation_count']==config['violation_threshold']: actions.add(config['threshold_action'])
  db.execute('INSERT INTO tgmate.moderation_evidence(tenant_id,group_config_id,message_event_id,violation_id,evidence_type,evidence_payload,content_sha256,retention_until) VALUES(%s,%s,%s,%s,\'rule_match\',%s,%s,now()+interval \'7 days\')',(tenant,config['id'],event['id'],violation,Jsonb({'rule_ids':[str(r['id']) for r in matches]}),event['content_hash']))
 if uncertain or 'review' in actions or 'ban' in actions and (not config['auto_ban_enabled'] or config['human_review_required_for_ban']):
  db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,violation_id,review_type) VALUES(%s,%s,%s,%s,'high_risk') ON CONFLICT DO NOTHING",(tenant,config['id'],event['id'],violation))
 actions.discard('review')
 if not config['auto_ban_enabled'] or config['human_review_required_for_ban']: actions.discard('ban')
 if config['cleanup_mode']=='log_only': actions={'log'} if matches else set()
 if 'ban' in actions: actions.discard('restrict')
 for action in sorted(actions,key=lambda a:['delete','warn','restrict','ban','log'].index(a)):
  kind={'delete':'delete_message','log':'log_only'}.get(action,action)
  queue_action(db,tenant,config,event,kind,'rule' if action in rule_actions or config['cleanup_mode']=='log_only' else 'threshold',violation)
 db.execute("UPDATE tgmate.moderation_event_processing SET processing_status='processed',processed_at=now(),attempt_count=attempt_count+1 WHERE message_event_id=%s",(event['id'],))
 return {'status':'processed','matched':len(matches),'actions':len(actions)}

# Welcome delivery is a distinct durable operation; evaluation never claims it was sent.
def prepare_join(db,tenant,event,config):
 welcome=db.execute('INSERT INTO tgmate.moderation_welcome_deliveries(tenant_id,group_config_id,telegram_user_id,join_event_key,welcome_status) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,group_config_id,join_event_key) DO UPDATE SET join_event_key=EXCLUDED.join_event_key RETURNING id',(tenant,config['id'],event['telegram_user_id'],event['external_event_key'],'pending' if config['welcome_enabled'] else 'skipped')).fetchone()['id']
 task=db.execute("INSERT INTO tgmate.task_registry(tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,'moderation',%s,'welcome','queued',%s) ON CONFLICT(tenant_id,business_module,idempotency_key) DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING id",(tenant,config['robot_instance_id'],f'welcome:{welcome}')).fetchone()['id']
 db.execute("INSERT INTO tgmate.outbox_events(tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,'moderation_welcome',%s,'moderation.welcome',%s,%s) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING",(tenant,welcome,f'welcome:{welcome}',Jsonb({'task_id':str(task),'event_id':str(event['id'])})))
 db.execute("UPDATE tgmate.moderation_event_processing SET processing_status='processed',processed_at=now() WHERE message_event_id=%s",(event['id'],))
 return {'status':'welcome_queued','welcome_id':str(welcome)}


def create_config(tenant,robot,binding,destination):
 with transaction(tenant) as db:
  e=db.execute("SELECT * FROM tgmate.executor_bindings WHERE id=%s AND robot_instance_id=%s AND purpose='group_manage' AND status='active'",(binding,robot)).fetchone()
  if not e or not e['telegram_bot_id']: raise Rejected('群管理执行绑定无效')
  if not db.execute("SELECT id FROM tgmate.telegram_destinations WHERE id=%s AND status='active'",(destination,)).fetchone(): raise Rejected('群组不存在')
  db.execute('INSERT INTO tgmate.bot_destination_access(tenant_id,telegram_bot_id,destination_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,e['telegram_bot_id'],destination))
  config=db.execute('INSERT INTO tgmate.moderation_group_configs(tenant_id,destination_id,robot_instance_id,telegram_bot_id,executor_binding_id,enabled) VALUES(%s,%s,%s,%s,%s,false) RETURNING id',(tenant,destination,robot,e['telegram_bot_id'],binding)).fetchone()['id']
  return {'config_id':str(config),'enabled':False}


def add_rule(tenant,config_id,code,name,kind,match_config,action='review',severity='medium',priority=100):
 validate_rule(kind,match_config)
 if action not in ('log','delete','warn','restrict','ban','review') or severity not in ('low','medium','high','critical'): raise Rejected('规则动作或严重程度无效')
 if not 1<=len(code)<=64 or not 1<=len(name)<=120 or type(priority) is not int or not 0<=priority<=10000: raise Rejected('规则配置无效')
 with transaction(tenant) as db:
  if not db.execute('SELECT id FROM tgmate.moderation_group_configs WHERE id=%s',(config_id,)).fetchone(): raise Rejected('未找到群管理配置')
  rule=db.execute('INSERT INTO tgmate.moderation_rules(tenant_id,group_config_id,rule_code,name,rule_type,match_config,action,severity,priority) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(tenant,config_id,code,name,kind,Jsonb(match_config),action,severity,priority)).fetchone()['id']
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'moderation.rule.created','moderation_rule',%s)",(tenant,rule))
  return {'rule_id':str(rule)}


def review(tenant,review_id,user_id,decision,note):
 if decision not in ('dismiss','log','delete','warn','restrict','ban') or not 10<=len(note.strip())<=1000: raise Rejected('需要审核决定和说明')
 with transaction(tenant) as db:
  if not user_id or not db.execute("SELECT user_id FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(user_id,)).fetchone(): raise Rejected('审核人必须是当前租户有效成员')
  item=db.execute('SELECT * FROM tgmate.moderation_review_queue WHERE id=%s FOR UPDATE',(review_id,)).fetchone()
  if not item or item['status'] not in ('pending','claimed'): raise Rejected('审核项不存在或已经处理')
  event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(item['message_event_id'],)).fetchone()
  c=db.execute('SELECT * FROM tgmate.moderation_group_configs WHERE id=%s',(item['group_config_id'],)).fetchone()
  if decision!='dismiss': queue_action(db,tenant,c,event,{'delete':'delete_message','log':'log_only'}.get(decision,decision),'admin',item['violation_id'],user_id,note)
  db.execute("UPDATE tgmate.moderation_review_queue SET status='resolved',assigned_to=%s,decision=%s,decision_note=%s,resolved_at=now() WHERE id=%s",(user_id,decision,note,review_id))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'moderation.review.resolved','moderation_review',%s,%s)",(tenant,user_id,review_id,Jsonb({'decision':decision,'note':note})))
  return {'review_id':str(review_id),'status':'resolved'}

# Manual resolution never converts an unknown mutation directly back to sending.
def resolve_action(tenant,action_id,user_id,decision,evidence):
 if decision not in ('confirm_succeeded','confirm_failed','cancel','retry') or not 10<=len(evidence.strip())<=1000: raise Rejected('需要核对决定及证据')
 with transaction(tenant) as db:
  if not user_id or not db.execute("SELECT user_id FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(user_id,)).fetchone(): raise Rejected('审核人身份无效')
  job=db.execute("SELECT * FROM tgmate.outbox_events WHERE event_type='moderation.action' AND aggregate_id=%s FOR UPDATE",(action_id,)).fetchone()
  if not job or job['status']=='publishing': raise Rejected('动作不存在或仍在执行')
  action=db.execute('SELECT * FROM tgmate.moderation_actions WHERE id=%s FOR UPDATE',(action_id,)).fetchone()
  if action['status'] in ('succeeded','cancelled'): raise Rejected('动作已结束')
  unknown=action['result'].get('unknown_outcome') is True
  if unknown and decision in ('retry','cancel'): raise Rejected('结果不明必须先核对成功或失败')
  if decision.startswith('confirm_') and not unknown: raise Rejected('该动作没有待核对的远端结果')
  from app.quota import settle
  if job['payload'].get('reservation_id'): settle(db,tenant,job['payload']['task_id'],decision=='confirm_succeeded',job['payload']['reservation_id'])
  if decision=='retry':
   if action['status'] not in ('failed','blocked','manual_review'): raise Rejected('当前动作不能重试')
   data=dict(job['payload']); data.pop('reservation_id',None); data['phase']='preflight'
   db.execute("UPDATE tgmate.moderation_actions SET status='pending',permission_check_status='pending',result='{}'::jsonb WHERE id=%s",(action_id,))
   db.execute("UPDATE tgmate.outbox_events SET status='pending',payload=%s,available_at=now(),locked_until=NULL WHERE id=%s",(Jsonb(data),job['id']))
   status='queued'
  else:
   outcome={'confirm_succeeded':'succeeded','confirm_failed':'failed','cancel':'cancelled'}[decision]
   db.execute('UPDATE tgmate.moderation_actions SET status=%s,result=%s WHERE id=%s',(outcome,Jsonb(dict(action['result'],unknown_outcome=False,manual_resolution=decision,**({'applied_until_date':action['result']['planned_until_date']} if decision=='confirm_succeeded' and action['action_type']=='restrict' and action['result'].get('planned_until_date') else {}))),action_id))
   status={'succeeded':'completed','failed':'failed','cancelled':'cancelled'}[outcome]
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',(status,job['payload']['task_id']))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'moderation.action.reviewed','moderation_action',%s,%s)",(tenant,user_id,action_id,Jsonb({'decision':decision,'evidence':evidence})))
  return {'action_id':str(action_id),'status':status}
