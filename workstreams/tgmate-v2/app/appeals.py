"""Identity-bound appeals, encrypted reasons and audited compensation requests."""
import hashlib,json,uuid
from psycopg.types.json import Jsonb
from app.db import transaction,seal
from app.relay import Rejected
from app.moderation import queue_action
from app.private_replies import enqueue

LABELS={'ban':'封禁','restrict':'限制发言','delete_message':'删除消息','warn':'警告'}


def eligible(db,bot,user,identifier):
 return db.execute("SELECT a.*,c.telegram_bot_id,c.robot_instance_id FROM tgmate.moderation_actions a JOIN tgmate.moderation_group_configs c ON c.tenant_id=a.tenant_id AND c.id=a.group_config_id WHERE a.id=%s AND a.telegram_user_id=%s AND c.telegram_bot_id=%s AND a.action_type IN ('ban','restrict','delete_message','warn') AND (a.status='succeeded' OR a.status='manual_review' AND a.result->>'unknown_outcome'='true')",(identifier,user,bot)).fetchone()


def receive(tenant,bot_id,update):
 if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<0: raise Rejected('事件格式错误')
 message=update.get('message'); callback=update.get('callback_query')
 if callback is not None:
  if not isinstance(callback,dict) or not isinstance(callback.get('data'),str) or not callback['data'].startswith('a:'): return None
  message=callback.get('message'); sender=callback.get('from')
 else: sender=message.get('from') if isinstance(message,dict) else None
 if not isinstance(message,dict) or not isinstance(sender,dict) or not isinstance(message.get('chat'),dict): return None
 user=sender.get('id'); chat=message['chat']
 if chat.get('type')!='private' or type(user) is not int or user<=0 or chat.get('id')!=user or sender.get('is_bot'): return None
 text=message.get('text','')
 if not isinstance(text,str): raise Rejected('申诉内容格式错误')
 command=text.split(maxsplit=1)[0].split('@')[0] if text.strip() else ''
 recognized=command=='/appeal' or command=='/start' and text.split(maxsplit=1)[-1].startswith('appeal_') or callback is not None
 with transaction(tenant) as db:
  db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'appeal:{tenant}:{bot_id}',))
  bot=db.execute("SELECT * FROM tgmate.telegram_bots WHERE id=%s AND status='active'",(bot_id,)).fetchone()
  if not bot: raise Rejected('机器人入口不可用')
  prompt=None
  if not recognized:
   reply=message.get('reply_to_message')
   if isinstance(reply,dict) and type(reply.get('message_id')) is int:
    prompt=db.execute("SELECT o.payload FROM tgmate.outbox_events o JOIN tgmate.task_deliveries d ON d.tenant_id=o.tenant_id AND d.task_id=(o.payload->>'task_id')::uuid WHERE o.event_type='bot.private_reply' AND o.aggregate_id=%s AND o.payload->>'kind'='appeal_prompt' AND (o.payload->>'target_user_id')::bigint=%s AND d.status='succeeded' AND d.remote_message_ref=%s AND o.created_at>now()-interval '1 day'",(bot_id,user,str(reply['message_id']))).fetchone()
   if not prompt:
    if isinstance(reply,dict):
     pending=db.execute("SELECT o.status,o.payload FROM tgmate.outbox_events o WHERE o.event_type='bot.private_reply' AND o.aggregate_id=%s AND o.payload->>'kind'='appeal_prompt' AND (o.payload->>'target_user_id')::bigint=%s AND (o.status='publishing' OR o.payload->>'unknown_outcome'='true') AND o.created_at>now()-interval '1 day' ORDER BY o.created_at DESC LIMIT 1",(bot_id,user)).fetchone()
     if pending and pending['status']=='publishing':
      from app.errors import DeferredIngress
      raise DeferredIngress()
     if pending:
      hints=db.execute("SELECT count(*) AS n FROM tgmate.outbox_events WHERE event_type='bot.private_reply' AND aggregate_id=%s AND payload->>'kind'='appeal_hint' AND (payload->>'target_user_id')::bigint=%s AND created_at>now()-interval '1 minute'",(bot_id,user)).fetchone()['n']
      if hints>=3: raise Rejected('操作过快，请稍后再试')
      config=db.execute('SELECT robot_instance_id FROM tgmate.moderation_group_configs WHERE telegram_bot_id=%s ORDER BY created_at LIMIT 1',(bot_id,)).fetchone()
      enqueue(db,tenant,bot_id,config['robot_instance_id'],user,'申诉提示消息的发送结果尚未确认。请重新发送 /appeal 后选择操作，再提交原因；当前回复未作为申诉提交。',f'appeal-hint:{bot_id}:{update["update_id"]}',kind='appeal_hint')
      return {'status':'awaiting_prompt_confirmation'}
    return None
  source=f'appeal:{bot_id}'; event=str(update['update_id']); digest=hashlib.sha256(json.dumps(update,sort_keys=True).encode()).hexdigest()
  old=db.execute('SELECT payload_hash FROM tgmate.inbox_events WHERE source_system=%s AND external_event_id=%s',(source,event)).fetchone()
  if old:
   if old['payload_hash']!=digest: raise Rejected('重复申诉事件冲突')
   return {'status':'duplicate'}
  recent=db.execute("SELECT count(*) AS n FROM tgmate.inbox_events WHERE source_system=%s AND payload->>'user_id'=%s AND received_at>now()-interval '1 minute'",(source,str(user))).fetchone()['n']
  if recent>=10: raise Rejected('操作过快，请稍后再试')
  config=db.execute('SELECT id,robot_instance_id FROM tgmate.moderation_group_configs WHERE telegram_bot_id=%s ORDER BY created_at LIMIT 1',(bot_id,)).fetchone()
  if not config: raise Rejected('该机器人未开通申诉入口')
  action_id=None; reason=None
  if prompt: action_id=uuid.UUID(prompt['payload']['action_id']); reason=text.strip()
  elif callback is not None:
   if not isinstance(message.get('from'),dict) or not bot['telegram_bot_id'] or message['from'].get('id')!=bot['telegram_bot_id']: raise Rejected('申诉按钮来源错误')
   try: action_id=uuid.UUID(hex=callback['data'][2:])
   except ValueError: raise Rejected('申诉按钮无效') from None
  elif command=='/start':
   try: action_id=uuid.UUID(text.split(maxsplit=1)[1].removeprefix('appeal_'))
   except (ValueError,IndexError): raise Rejected('申诉入口无效') from None
  else:
   parts=text.split(maxsplit=2)
   if len(parts)>=2:
    try: action_id=uuid.UUID(parts[1])
    except ValueError: raise Rejected('申诉编号无效') from None
   if len(parts)==3: reason=parts[2].strip()
  prefix=f'appeal-reply:{bot_id}:{event}'
  if action_id is None:
   rows=db.execute("SELECT a.id,a.action_type FROM tgmate.moderation_actions a JOIN tgmate.moderation_group_configs c ON c.tenant_id=a.tenant_id AND c.id=a.group_config_id WHERE c.telegram_bot_id=%s AND a.telegram_user_id=%s AND (a.status='succeeded' OR a.status='manual_review' AND a.result->>'unknown_outcome'='true') AND a.action_type IN ('ban','restrict','delete_message','warn') ORDER BY a.created_at DESC LIMIT 10",(bot_id,user)).fetchall()
   markup={'inline_keyboard':[[{'text':f"申诉：{LABELS[r['action_type']]}",'callback_data':f"a:{r['id'].hex}"}] for r in rows]} if rows else None
   enqueue(db,tenant,bot_id,config['robot_instance_id'],user,'请选择需要申诉的操作。' if rows else '暂无可申诉的操作。',prefix,markup=markup)
   status='menu_queued'
  else:
   action=eligible(db,bot_id,user,action_id)
   if not action: raise Rejected('该申诉操作不可用')
   if reason is None:
    enqueue(db,tenant,bot_id,action['robot_instance_id'],user,'请回复本条消息，说明申诉原因。',prefix,kind='appeal_prompt',action_id=action_id,markup={'force_reply':True,'selective':True,'input_field_placeholder':'请输入申诉原因'})
    if callback is not None:
     from app.verification import enqueue_ack
     context=db.execute('SELECT * FROM tgmate.moderation_group_configs WHERE id=%s',(action['group_config_id'],)).fetchone()
     enqueue_ack(db,tenant,context,{'id':action_id},callback.get('id'),'appeal_prompt',aggregate_type='moderation_action')
    status='prompt_queued'
   else:
    if not 3<=len(reason)<=2000: raise Rejected('申诉原因需为3至2000字')
    existing=db.execute('SELECT id,status FROM tgmate.moderation_appeals WHERE action_id=%s AND telegram_user_id=%s',(action_id,user)).fetchone()
    appeal=None
    if not existing:
     payload=db.execute("INSERT INTO tgmate.relay_payloads(tenant_id,ciphertext,expires_at) VALUES(%s,%s,now()+interval '30 days') RETURNING id",(tenant,seal({'reason':reason}))).fetchone()['id']
     appeal=db.execute('INSERT INTO tgmate.moderation_appeals(tenant_id,group_config_id,action_id,telegram_user_id,appeal_text) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,action_id,telegram_user_id) DO NOTHING RETURNING id',(tenant,action['group_config_id'],action_id,user,f'vault:{payload}')).fetchone()
    if appeal:
     db.execute("INSERT INTO tgmate.moderation_review_queue(tenant_id,group_config_id,message_event_id,violation_id,review_type) VALUES(%s,%s,%s,%s,'appeal') ON CONFLICT DO NOTHING",(tenant,action['group_config_id'],action['message_event_id'],action['violation_id']))
     db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id) VALUES(%s,'moderation.appeal.submitted','moderation_appeal',%s)",(tenant,appeal['id']))
    feedback='申诉已提交，请等待管理员审核。' if appeal else {'accepted':'该申诉已通过，请查看管理员的审核结果。','rejected':'该申诉已审核，结果未通过。','withdrawn':'原申诉已撤回，如需重新提交请联系管理员。'}.get(existing['status'] if existing else '', '该操作已有申诉，请等待原申诉的审核结果。')
    enqueue(db,tenant,bot_id,action['robot_instance_id'],user,feedback,prefix)
    status='submitted' if appeal else 'already_submitted'
  db.execute("INSERT INTO tgmate.inbox_events(tenant_id,source_system,external_event_id,payload_hash,payload,status,processed_at) VALUES(%s,%s,%s,%s,%s,'processed',now())",(tenant,source,event,digest,Jsonb({'user_id':user,'action_id':str(action_id) if action_id else None})))
  return {'status':status}

# Counter refunds are append-only audit decisions; original violations/actions remain intact.
def refund_counter(db,tenant,action,user_id):
 if not action['violation_id']: return
 violation=db.execute('SELECT * FROM tgmate.moderation_violations WHERE id=%s',(action['violation_id'],)).fetchone()
 if not violation or not violation['counted_in_window']: return
 record=db.execute("INSERT INTO tgmate.idempotency_records(tenant_id,scope,idempotency_key,request_hash,response_status,response_body,expires_at) VALUES(%s,'appeal_counter_refund',%s,%s,200,%s,%s+interval '1 day') ON CONFLICT(tenant_id,scope,idempotency_key) DO NOTHING RETURNING id",(tenant,str(violation['id']),str(violation['id']),Jsonb({'processed':True}),violation['window_ends_at'])).fetchone()
 if not record: return
 changed=db.execute('UPDATE tgmate.moderation_violation_counters SET violation_count=greatest(0,violation_count-1) WHERE group_config_id=%s AND telegram_user_id=%s AND window_started_at=%s AND window_ends_at=%s AND window_ends_at>now() RETURNING violation_count',(violation['group_config_id'],violation['telegram_user_id'],violation['window_started_at'],violation['window_ends_at'])).fetchone()
 db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'moderation.counter.appeal_adjustment','moderation_violation',%s,%s)",(tenant,user_id,violation['id'],Jsonb({'adjusted_current_window':bool(changed)})))


def review(tenant,appeal_id,user_id,decision,note):
 if decision not in ('accept','reject') or not 10<=len(note.strip())<=1000: raise Rejected('请提供申诉审核决定和说明')
 with transaction(tenant) as db:
  if not user_id or not db.execute("SELECT user_id FROM tgmate.tenant_members WHERE user_id=%s AND status='active'",(user_id,)).fetchone(): raise Rejected('审核人身份无效')
  appeal=db.execute('SELECT * FROM tgmate.moderation_appeals WHERE id=%s FOR UPDATE',(appeal_id,)).fetchone()
  if not appeal or appeal['status'] not in ('submitted','under_review'): raise Rejected('申诉不存在或已审核')
  action=db.execute('SELECT * FROM tgmate.moderation_actions WHERE id=%s FOR UPDATE',(appeal['action_id'],)).fetchone()
  config=db.execute('SELECT * FROM tgmate.moderation_group_configs WHERE id=%s',(appeal['group_config_id'],)).fetchone()
  followup=None
  if decision=='accept':
   refund_counter(db,tenant,action,user_id)
   inverse={'ban':'unban','restrict':'unrestrict'}.get(action['action_type'])
   if inverse:
    event=db.execute('SELECT * FROM tgmate.moderation_message_events WHERE id=%s',(action['message_event_id'],)).fetchone()
    followup=queue_action(db,tenant,config,event,inverse,'admin',action['violation_id'],user_id,f"appeal:{action['id']}")
  status='accepted' if decision=='accept' else 'rejected'
  db.execute('UPDATE tgmate.moderation_appeals SET status=%s,reviewer_user_id=%s,reviewer_note=%s,reviewed_at=now() WHERE id=%s',(status,user_id,note,appeal_id))
  # Other appeal items for the same event may still need review.
  remaining=db.execute("SELECT a.id FROM tgmate.moderation_appeals a JOIN tgmate.moderation_actions x ON x.tenant_id=a.tenant_id AND x.id=a.action_id WHERE x.message_event_id=%s AND a.status IN ('submitted','under_review') LIMIT 1",(action['message_event_id'],)).fetchone()
  if not remaining: db.execute("UPDATE tgmate.moderation_review_queue SET status='resolved',assigned_to=%s,decision=%s,decision_note=%s,resolved_at=now() WHERE message_event_id=%s AND review_type='appeal' AND status IN ('pending','claimed')",(user_id,decision,note,action['message_event_id']))
  text='申诉已受理通过。恢复操作已排队，具体结果以执行记录为准。' if followup else '申诉审核通过。警告或删除记录的申诉决定已保存，已删除内容不会自动重发。' if decision=='accept' else '申诉未通过。请联系管理员查看审核说明。'
  enqueue(db,tenant,config['telegram_bot_id'],config['robot_instance_id'],appeal['telegram_user_id'],text,f'appeal-decision:{appeal_id}')
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,metadata) VALUES(%s,%s,'moderation.appeal.reviewed','moderation_appeal',%s,%s)",(tenant,user_id,appeal_id,Jsonb({'decision':decision,'note':note,'compensation_action_id':str(followup) if followup else None})))
  return {'appeal_id':str(appeal_id),'status':status,'compensation_action_id':str(followup) if followup else None}
