import uuid,psycopg,pytest
from psycopg.types.json import Jsonb
from conftest import DB
from test_service import service
from test_moderation import moderation,Adapter,message,rule,work
from app.db import transaction,settings,unseal
from app.appeals import receive,review
from app.private_worker import run_once
from app.relay import Rejected

class Replies(Adapter):
 def __init__(self): super().__init__(); self.replies=[]
 def send_private(self,bot,user,text,markup=None): self.replies.append((user,text,markup)); return 800+len(self.replies)
 def execute(self,config,action,message):
  self.executed.append(action['action_type'])
  if action['action_type']=='restrict': return {'ok':True,'applied_until_date':2000000000,'restriction_fingerprint':'test-owned'}
  if action['action_type']=='ban': return {'ok':True,'ban_fingerprint':'test-ban'}
  if action['action_type']=='unrestrict': assert action['restore_until_date']==2000000000 and action['restore_fingerprint']=='test-owned'
  if action['action_type']=='unban': assert action['restore_ban_fingerprint']=='test-ban'
  return {'ok':True}


def action(a,kind='delete',adapter=None):
 adapter=adapter or Replies(); rule(a,action=kind)
 if kind=='ban':
  with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.moderation_group_configs SET auto_ban_enabled=true,human_review_required_for_ban=false WHERE id=%s',(a['group'],))
 receive_message=__import__('app.moderation',fromlist=['receive']).receive
 receive_message(a['tenant'],a['group'],message()); work(a,adapter); work(a,adapter)
 with transaction(a['tenant']) as db: return db.execute('SELECT id FROM tgmate.moderation_actions').fetchone()['id']


def private(event,text,user=200,reply=None):
 data={'update_id':event,'message':{'message_id':event,'chat':{'id':user,'type':'private'},'from':{'id':user,'is_bot':False},'text':text}}
 if reply: data['message']['reply_to_message']={'message_id':reply}
 return data


def deliver(a,adapter): return run_once(a['tenant'],settings()['worker_id'],adapter)


def test_appeal_is_identity_bound_and_encrypted(moderation):
 a=moderation; identifier=action(a)
 assert receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 请重新核对删除原因'))['status']=='submitted'
 with pytest.raises(Rejected): receive(a['tenant'],a['modbot'],private(2,f'/appeal {identifier} 冒用其他用户提交申诉',201))
 with transaction(a['tenant']) as db:
  row=db.execute('SELECT * FROM tgmate.moderation_appeals').fetchone()
  assert row['appeal_text'].startswith('vault:')
  payload=db.execute('SELECT ciphertext FROM tgmate.relay_payloads WHERE id=%s',(row['appeal_text'][6:],)).fetchone()['ciphertext']
  assert unseal(payload)['reason']=='请重新核对删除原因'


def test_appeal_dedup_and_no_duplicate_request(moderation):
 a=moderation; identifier=action(a); data=private(1,f'/appeal {identifier} 请管理员重新核对')
 assert receive(a['tenant'],a['modbot'],data)['status']=='submitted'
 assert receive(a['tenant'],a['modbot'],data)['status']=='duplicate'
 assert receive(a['tenant'],a['modbot'],private(2,f'/appeal {identifier} 我再提交相同操作的申诉'))['status']=='already_submitted'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_appeals').fetchone()['n']==1


def test_menu_prompt_reply_pipeline(moderation):
 a=moderation; identifier=action(a); adapter=Replies()
 with psycopg.connect(**DB) as db: db.execute('UPDATE tgmate.telegram_bots SET telegram_bot_id=44 WHERE id=%s',(a['modbot'],))
 assert receive(a['tenant'],a['modbot'],private(1,'/appeal'))['status']=='menu_queued'; assert deliver(a,adapter)['status']=='succeeded'
 callback={'update_id':2,'callback_query':{'id':'appeal-callback','data':f'a:{identifier.hex}','from':{'id':200,'is_bot':False},'message':{'message_id':801,'chat':{'id':200,'type':'private'},'from':{'id':44,'is_bot':True}}}}
 assert receive(a['tenant'],a['modbot'],callback)['status']=='prompt_queued'; assert deliver(a,adapter)['status']=='succeeded'
 assert adapter.replies[-1][2]['force_reply'] is True
 assert receive(a['tenant'],a['modbot'],private(3,'请核实上下文，这条消息并非广告',reply=802))['status']=='submitted'


def test_accept_restrict_queues_safe_compensation(moderation):
 a=moderation; adapter=Replies(); identifier=action(a,'restrict',adapter)
 receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 请重新核对限制发言原因'))
 with transaction(a['tenant']) as db: appeal=db.execute('SELECT id FROM tgmate.moderation_appeals').fetchone()['id']
 result=review(a['tenant'],appeal,a['user'],'accept','管理员核对证据后认为原处罚不当，同意恢复')
 assert result['compensation_action_id']
 assert work(a,adapter)['status']=='succeeded'; assert adapter.executed[-1]=='unrestrict'
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.moderation_appeals').fetchone()['status']=='accepted'
  assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==0
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==1


def test_reject_does_not_change_violation(moderation):
 a=moderation; identifier=action(a); receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 请重新核实删除理由'))
 with transaction(a['tenant']) as db: appeal=db.execute('SELECT id FROM tgmate.moderation_appeals').fetchone()['id']
 assert review(a['tenant'],appeal,a['user'],'reject','管理员复核后确认该违规成立，维持原处理')['status']=='rejected'
 with transaction(a['tenant']) as db: assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==1


def test_unknown_private_reply_is_not_blindly_resent(moderation):
 from app.worker import UnknownSend
 a=moderation; action(a); receive(a['tenant'],a['modbot'],private(1,'/appeal'))
 class Broken(Replies):
  def send_private(self,*args): raise UnknownSend()
 assert deliver(a,Broken())['status']=='unknown'
 assert deliver(a,Replies())['status']=='idle'


def test_nonmember_cannot_review(moderation):
 a=moderation; identifier=action(a); receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 请重新核实处理原因'))
 with transaction(a['tenant']) as db: appeal=db.execute('SELECT id FROM tgmate.moderation_appeals').fetchone()['id']
 with pytest.raises(Rejected): review(a['tenant'],appeal,uuid.uuid4(),'accept','不属于该租户的成员不能处理任何申诉')

def test_accepted_ban_queues_verified_unban(moderation):
 a=moderation; adapter=Replies(); identifier=action(a,'ban',adapter)
 receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 请求复核封禁操作'))
 with transaction(a['tenant']) as db: appeal=db.execute('SELECT id FROM tgmate.moderation_appeals').fetchone()['id']
 review(a['tenant'],appeal,a['user'],'accept','管理员人工核实该封禁操作不当，同意解除')
 assert work(a,adapter)['status']=='succeeded'; assert adapter.executed[-1]=='unban'


def test_two_actions_same_violation_adjust_counter_once(moderation):
 a=moderation; adapter=Replies(); rule(a,code='one',action='delete'); rule(a,code='two',action='warn')
 __import__('app.moderation',fromlist=['receive']).receive(a['tenant'],a['group'],message()); work(a,adapter); work(a,adapter); work(a,adapter)
 with transaction(a['tenant']) as db: identifiers=[r['id'] for r in db.execute('SELECT id FROM tgmate.moderation_actions').fetchall()]
 for n,identifier in enumerate(identifiers,1): receive(a['tenant'],a['modbot'],private(n,f'/appeal {identifier} 请求复核该操作'))
 with transaction(a['tenant']) as db: appeals=[r['id'] for r in db.execute('SELECT id FROM tgmate.moderation_appeals').fetchall()]
 for appeal in appeals: review(a['tenant'],appeal,a['user'],'accept','管理员已核实同一事件的两项处理均不合理')
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==0
  assert db.execute("SELECT count(*) AS n FROM tgmate.audit_logs WHERE action='moderation.counter.appeal_adjustment'").fetchone()['n']==1


def test_old_appeal_cannot_decrement_new_window(moderation):
 a=moderation; identifier=action(a); receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 请求重新核实原窗口内的处理'))
 with transaction(a['tenant']) as db:
  appeal=db.execute('SELECT id FROM tgmate.moderation_appeals').fetchone()['id']
  db.execute("UPDATE tgmate.moderation_violation_counters SET window_started_at=now(),window_ends_at=now()+interval '1 day',violation_count=1")
 review(a['tenant'],appeal,a['user'],'accept','管理员接受原窗口事件的申诉，不影响新窗口')
 with transaction(a['tenant']) as db: assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==1


def test_unknown_prompt_reply_is_not_forwarded_to_customer_service(moderation):
 from app.worker import UnknownSend
 a=moderation; identifier=action(a)
 receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier}'))
 class Broken(Replies):
  def send_private(self,*args): raise UnknownSend()
 assert deliver(a,Broken())['status']=='unknown'
 assert receive(a['tenant'],a['modbot'],private(2,'这份私密申诉原因不应转交普通客服',reply=999))['status']=='awaiting_prompt_confirmation'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_appeals').fetchone()['n']==0


def test_private_prompt_can_be_confirmed_then_reply_is_correlated(moderation):
 from app.worker import UnknownSend
 from app.private_replies import review_reply
 a=moderation; identifier=action(a); receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier}'))
 class Broken(Replies):
  def send_private(self,*args): raise UnknownSend()
 deliver(a,Broken())
 with transaction(a['tenant']) as db: job=db.execute("SELECT id FROM tgmate.outbox_events WHERE event_type='bot.private_reply'").fetchone()['id']
 with pytest.raises(Rejected): review_reply(a['tenant'],job,a['user'],'retry','没有远端结果前不能直接重复发送提示消息')
 review_reply(a['tenant'],job,a['user'],'confirm_sent','管理员已确认该提示消息发送成功并核对编号',801)
 assert receive(a['tenant'],a['modbot'],private(2,'请重新检查消息上下文',reply=801))['status']=='submitted'

def test_user_can_appeal_unknown_action_but_it_is_not_blindly_undone(moderation):
 from app.worker import UnknownSend
 a=moderation
 class Broken(Replies):
  def execute(self,*args): raise UnknownSend()
 identifier=action(a,'restrict',Broken())
 assert receive(a['tenant'],a['modbot'],private(1,f'/appeal {identifier} 我已被限制，请检查未决操作'))['status']=='submitted'
 with transaction(a['tenant']) as db: appeal=db.execute('SELECT id FROM tgmate.moderation_appeals').fetchone()['id']
 review(a['tenant'],appeal,a['user'],'accept','管理员接受申诉，但原动作结果需先核对后才能恢复')
 assert work(a,Replies())['status']=='manual'
