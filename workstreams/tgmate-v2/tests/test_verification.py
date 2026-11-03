import uuid,psycopg,pytest
from psycopg.types.json import Jsonb
from conftest import DB
from test_service import service
from test_moderation import moderation,Adapter,message,work
from app.db import transaction,settings
from app.moderation import receive
from app.verification import callback,expire_sessions
from app.verification_worker import run_once
from app.worker import UnknownSend,RetryLater

class VerificationAdapter(Adapter):
 def __init__(self): super().__init__(); self.notices=[]; self.restricted={}; self.changed=False
 def member_role(self,config,user): return 'restricted' if user in self.restricted else 'member'
 def execute(self,config,action,message):
  self.executed.append(action['action_type']); user=action['telegram_user_id']
  if action['action_type']=='restrict':
   self.restricted[user]=action['until_date']; return {'ok':True,'applied_until_date':action['until_date'],'restriction_fingerprint':'owned-test-fingerprint'}
  if action['action_type']=='unrestrict':
   from app.worker import PermanentFailure
   if self.changed: raise PermanentFailure('Restriction changed')
   assert action['restore_until_date']==self.restricted[user]
   assert action['restore_fingerprint']=='owned-test-fingerprint'
   self.restricted.pop(user); return {'ok':True}
  return {'ok':True}
 def send_notice(self,config,text,markup=None): self.notices.append((text,markup)); return 700+len(self.notices)


def join(a,adapter,restrict=True,success=True,event=1,user=300):
 with psycopg.connect(**DB) as db: db.execute('UPDATE tgmate.telegram_bots SET telegram_bot_id=44 WHERE id=%s',(a['modbot'],))
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.moderation_group_configs SET temporary_restrict_on_join=%s,verification_success_payload=%s WHERE id=%s',(restrict,Jsonb({'text':'验证成功'}) if success else None,a['group']))
 data=message(event,text='',user=200); data['message']['new_chat_members']=[{'id':user,'is_bot':False}]
 receive(a['tenant'],a['group'],data); assert work(a,adapter)['status']=='welcome_queued'
 assert notice(a,adapter)['status']=='succeeded'
 with transaction(a['tenant']) as db: return db.execute('SELECT * FROM tgmate.moderation_verification_sessions WHERE join_event_key=%s',(f'{event}:join:{user}',)).fetchone()


def notice(a,adapter): return run_once(a['tenant'],settings()['worker_id'],adapter)


def callback_update(adapter,event=100,user=300):
 index=next(i for i,n in enumerate(adapter.notices) if n[1])
 data=adapter.notices[index][1]['inline_keyboard'][0][0]['callback_data']
 return {'update_id':event,'callback_query':{'id':f'callback-{event}','from':{'id':user,'is_bot':False},'data':data,'message':{'message_id':701+index,'chat':{'id':-100888,'type':'supergroup'},'from':{'id':44,'is_bot':True}}}}


def test_welcome_restrict_verify_restore_then_success(moderation):
 a=moderation; adapter=VerificationAdapter(); session=join(a,adapter)
 assert len(adapter.notices)==1 and 300 not in adapter.restricted
 assert work(a,adapter)['status']=='succeeded'; assert 300 in adapter.restricted
 assert notice(a,adapter)['notice']=='moderation.challenge'
 response=callback(a['tenant'],a['group'],callback_update(adapter)); assert response['status']=='passed'
 assert notice(a,adapter)['status']=='waiting_restore'
 assert work(a,adapter)['status']=='succeeded'; assert 300 not in adapter.restricted
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.outbox_events SET available_at=now() WHERE event_type='moderation.success'")
 assert notice(a,adapter)['notice']=='moderation.success'
 assert len(adapter.notices)==3
 with transaction(a['tenant']) as db:
  row=db.execute('SELECT * FROM tgmate.moderation_welcome_deliveries').fetchone()
  assert row['welcome_status']=='sent' and row['verification_status']=='passed' and row['success_message_status']=='sent'


def test_callback_wrong_person_does_not_consume_attempt(moderation):
 from app.relay import Rejected
 a=moderation; adapter=VerificationAdapter(); session=join(a,adapter,restrict=False); notice(a,adapter)
 with pytest.raises(Rejected): callback(a['tenant'],a['group'],callback_update(adapter,user=301))
 with transaction(a['tenant']) as db: assert db.execute('SELECT attempt_count FROM tgmate.moderation_verification_sessions').fetchone()['attempt_count']==0


def test_callback_is_idempotent_and_user_bound(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter,restrict=False); notice(a,adapter)
 update=callback_update(adapter)
 assert callback(a['tenant'],a['group'],update)['status']=='passed'
 assert callback(a['tenant'],a['group'],update)['status']=='duplicate'
 with transaction(a['tenant']) as db: assert db.execute('SELECT attempt_count FROM tgmate.moderation_verification_sessions').fetchone()['attempt_count']==1


def test_expiry_queues_recovery_without_ban(moderation):
 a=moderation; adapter=VerificationAdapter(); session=join(a,adapter); work(a,adapter); notice(a,adapter)
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.moderation_verification_sessions SET expires_at=now()-interval '1 second',created_at=now()-interval '2 minutes'")
 assert expire_sessions(a['tenant'])==1
 assert work(a,adapter)['status']=='succeeded'; assert not adapter.restricted
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.moderation_verification_sessions').fetchone()['status']=='expired'
  assert db.execute("SELECT count(*) AS n FROM tgmate.moderation_actions WHERE action_type='ban'").fetchone()['n']==0


def test_pass_before_restrict_prevents_late_restriction(moderation):
 a=moderation; adapter=VerificationAdapter(); session=join(a,adapter,restrict=False)
 notice(a,adapter); callback(a['tenant'],a['group'],callback_update(adapter))
 assert work(a,adapter)['status']=='cancel'
 assert not adapter.restricted


def test_changed_admin_restriction_is_not_overwritten(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter); work(a,adapter); notice(a,adapter)
 callback(a['tenant'],a['group'],callback_update(adapter)); adapter.changed=True
 assert work(a,adapter)['status']=='failed'
 assert 300 in adapter.restricted


def test_unknown_welcome_does_not_start_verification_or_replay(moderation):
 a=moderation; adapter=VerificationAdapter()
 data=message(); data['message']['new_chat_members']=[{'id':300,'is_bot':False}]
 receive(a['tenant'],a['group'],data); work(a,adapter)
 class Broken(VerificationAdapter):
  def send_notice(self,*args): raise UnknownSend()
 assert notice(a,Broken())['status']=='unknown'
 assert notice(a,adapter)['status']=='idle'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_verification_sessions').fetchone()['n']==0


def test_restore_is_not_blocked_by_exhausted_quota(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter); work(a,adapter); notice(a,adapter)
 callback(a['tenant'],a['group'],callback_update(adapter))
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.quota_accounts SET limit_value=reserved_value+consumed_value WHERE entitlement_code='moderation.actions'")
 assert work(a,adapter)['status']=='succeeded'; assert not adapter.restricted

def test_bad_nonce_max_attempts_fails_and_restores(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter); work(a,adapter); notice(a,adapter)
 for i in range(3):
  update=callback_update(adapter,event=200+i)
  parts=update['callback_query']['data'].split(':'); parts[2]='wrong-token'; update['callback_query']['data']=':'.join(parts)
  assert callback(a['tenant'],a['group'],update)['status']=='failed'
 assert work(a,adapter)['status']=='succeeded'
 assert not adapter.restricted
 with transaction(a['tenant']) as db: assert db.execute('SELECT status,attempt_count FROM tgmate.moderation_verification_sessions').fetchone()=={'status':'failed','attempt_count':3}


def test_wrong_group_and_wrong_message_rejected(moderation):
 from app.relay import Rejected
 a=moderation; adapter=VerificationAdapter(); join(a,adapter,restrict=False); notice(a,adapter)
 for field,value in [('chat',{'id':-100999}),('message_id',999),('from',{'id':45})]:
  update=callback_update(adapter); update['callback_query']['message'][field]=value
  with pytest.raises(Rejected): callback(a['tenant'],a['group'],update)


def test_success_notice_once_and_callback_has_no_plain_nonce_storage(moderation):
 import json
 a=moderation; adapter=VerificationAdapter(); join(a,adapter,restrict=False); notice(a,adapter)
 update=callback_update(adapter); nonce=update['callback_query']['data'].split(':')[2]
 callback(a['tenant'],a['group'],update); work(a,adapter)
 assert notice(a,adapter)['status']=='succeeded'
 assert notice(a,adapter)['status']=='idle'
 with transaction(a['tenant']) as db:
  data=db.execute('SELECT challenge_payload FROM tgmate.moderation_verification_sessions').fetchone()['challenge_payload']
  assert nonce not in json.dumps(data)


def test_disabled_config_still_allows_own_restriction_cleanup(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter); work(a,adapter); notice(a,adapter)
 callback(a['tenant'],a['group'],callback_update(adapter))
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.moderation_group_configs SET enabled=false WHERE id=%s',(a['group'],))
 assert work(a,adapter)['status']=='succeeded'; assert not adapter.restricted


def test_manual_review_notice_unknown_needs_evidence_then_starts_session(moderation):
 from app.verification import resolve_notice
 from app.relay import Rejected
 a=moderation; adapter=VerificationAdapter(); data=message(); data['message']['new_chat_members']=[{'id':300,'is_bot':False}]
 receive(a['tenant'],a['group'],data); work(a,adapter)
 class Broken(VerificationAdapter):
  def send_notice(self,*args): raise UnknownSend()
 notice(a,Broken())
 with transaction(a['tenant']) as db: job=db.execute("SELECT id FROM tgmate.outbox_events WHERE event_type='moderation.welcome'").fetchone()['id']
 with pytest.raises(Rejected): resolve_notice(a['tenant'],job,a['user'],'retry','没有确认投递结果前不能通过重发来猜测')
 assert resolve_notice(a['tenant'],job,a['user'],'confirm_sent','管理员已检查群组并确认欢迎消息已成功发送',801)['status']=='completed'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_verification_sessions').fetchone()['n']==1


def test_unknown_challenge_triggers_owned_cleanup(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter); work(a,adapter)
 def broken(*args): raise UnknownSend()
 adapter.send_notice=broken
 assert notice(a,adapter)['status']=='unknown'
 assert work(a,adapter)['status']=='succeeded'; assert not adapter.restricted
 with transaction(a['tenant']) as db: assert db.execute('SELECT status FROM tgmate.moderation_verification_sessions').fetchone()['status']=='manual_review'

def test_quota_exhaustion_after_restrict_still_restores(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter)
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.quota_accounts SET limit_value=2 WHERE entitlement_code='moderation.actions'")
 assert work(a,adapter)['status']=='succeeded'
 assert notice(a,adapter)['status']=='quota_exceeded'
 assert work(a,adapter)['status']=='succeeded'; assert not adapter.restricted


def test_notice_expired_lease_is_not_replayed(moderation):
 a=moderation; adapter=VerificationAdapter(); data=message(); data['message']['new_chat_members']=[{'id':300,'is_bot':False}]
 receive(a['tenant'],a['group'],data); work(a,adapter)
 class Expired(VerificationAdapter):
  def send_notice(self,*args):
   with transaction(a['tenant']) as db:
    db.execute("UPDATE tgmate.task_leases SET leased_at=now()-interval '2 minutes',lease_until=now()-interval '1 minute'")
    db.execute("UPDATE tgmate.outbox_events SET locked_until=now()-interval '1 minute' WHERE event_type='moderation.welcome'")
   return 900
 assert notice(a,Expired())['status']=='fenced'
 assert notice(a,adapter)['status']=='idle'; assert not adapter.notices
 with transaction(a['tenant']) as db: assert db.execute("SELECT status FROM tgmate.task_deliveries").fetchone()['status']=='unknown'


def test_disabled_restrict_before_challenge_is_skipped(moderation):
 a=moderation; adapter=VerificationAdapter(); session=join(a,adapter)
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.moderation_verification_sessions SET status='passed' WHERE id=%s",(session['id'],))
 assert work(a,adapter)['status']=='cancel'
 assert 300 not in adapter.restricted


def test_restore_failure_does_not_send_misleading_success(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter); work(a,adapter); notice(a,adapter)
 callback(a['tenant'],a['group'],callback_update(adapter)); adapter.changed=True; work(a,adapter)
 assert notice(a,adapter)['status']=='manual_review'
 assert len(adapter.notices)==2

def test_invalid_welcome_does_not_loop_or_spend_quota(moderation):
 a=moderation; adapter=VerificationAdapter()
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.moderation_group_configs SET welcome_payload=%s WHERE id=%s',(Jsonb({'text':[]} ),a['group']))
 data=message(); data['message']['new_chat_members']=[{'id':300,'is_bot':False}]
 receive(a['tenant'],a['group'],data); work(a,adapter)
 assert notice(a,adapter)['status']=='manual_review'
 assert notice(a,adapter)['status']=='idle'
 assert not adapter.notices
 with transaction(a['tenant']) as db: assert db.execute("SELECT reserved_value,consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='moderation.actions'").fetchone()=={'reserved_value':0,'consumed_value':0}

def test_callback_ack_is_queued_once_without_affecting_decision(moderation):
 from app.verification_ack import run_once as ack_once
 a=moderation; adapter=VerificationAdapter(); join(a,adapter,restrict=False); notice(a,adapter)
 update=callback_update(adapter); callback(a['tenant'],a['group'],update); callback(a['tenant'],a['group'],update)
 answers=[]
 adapter.answer_callback=lambda config,query,text: answers.append((query,text)) or True
 assert ack_once(a['tenant'],settings()['worker_id'],adapter)['status']=='completed'
 assert ack_once(a['tenant'],settings()['worker_id'],adapter)['status']=='idle'
 assert len(answers)==1
 with transaction(a['tenant']) as db: assert db.execute('SELECT status FROM tgmate.moderation_verification_sessions').fetchone()['status']=='passed'


def test_callback_ack_failure_does_not_undo_verification(moderation):
 from app.verification_ack import run_once as ack_once
 from app.worker import PermanentFailure
 a=moderation; adapter=VerificationAdapter(); join(a,adapter,restrict=False); notice(a,adapter); callback(a['tenant'],a['group'],callback_update(adapter))
 def rejected(*args): raise PermanentFailure()
 adapter.answer_callback=rejected
 assert ack_once(a['tenant'],settings()['worker_id'],adapter)['status']=='failed'
 with transaction(a['tenant']) as db: assert db.execute('SELECT status FROM tgmate.moderation_verification_sessions').fetchone()['status']=='passed'
