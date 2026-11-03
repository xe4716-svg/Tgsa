import uuid,psycopg,pytest
from psycopg.types.json import Jsonb
from conftest import DB
from test_service import service
from app.db import transaction,settings
from app.moderation import receive
from app.moderation_worker import run_once

@pytest.fixture
def moderation(service):
 client,a,c=service
 r,b,e,d,g=[uuid.uuid4() for _ in range(5)]
 with psycopg.connect(**DB) as db:
  db.execute("INSERT INTO tgmate.robot_instances(id,tenant_id,product_type_code,name) VALUES(%s,%s,'moderation','test moderation')",(r,a['tenant']))
  db.execute("INSERT INTO tgmate.telegram_bots(id,tenant_id,token_vault_ref,token_fingerprint,status) VALUES(%s,%s,'test-only',%s,'active')",(b,a['tenant'],str(b)))
  db.execute("INSERT INTO tgmate.executor_bindings(id,tenant_id,robot_instance_id,telegram_bot_id,purpose) VALUES(%s,%s,%s,%s,'group_manage')",(e,a['tenant'],r,b))
  db.execute("INSERT INTO tgmate.telegram_destinations(id,tenant_id,telegram_peer_id,destination_type,title) VALUES(%s,%s,-100888,'supergroup','moderation test')",(d,a['tenant']))
  db.execute("INSERT INTO tgmate.bot_destination_access(tenant_id,telegram_bot_id,destination_id,membership_status) VALUES(%s,%s,%s,'member')",(a['tenant'],b,d))
  db.execute('INSERT INTO tgmate.moderation_group_configs(id,tenant_id,destination_id,robot_instance_id,telegram_bot_id,executor_binding_id) VALUES(%s,%s,%s,%s,%s,%s)',(g,a['tenant'],d,r,b,e))
  db.execute("INSERT INTO tgmate.quota_accounts(tenant_id,entitlement_code,period_start,period_end,limit_value) VALUES(%s,'moderation.actions',now()-interval '1 hour',now()+interval '30 days',100)",(a['tenant'],))
 return dict(a,group=g,modbot=b,modrobot=r,destination=d)

class Adapter:
 def __init__(self,role='member',allowed=True): self.role=role; self.allowed=allowed; self.executed=[]
 def member_role(self,config,user): return self.role
 def permissions(self,config): return dict(membership_status='member',can_send_messages=self.allowed,can_delete_messages=self.allowed,can_restrict_members=self.allowed,can_ban_members=self.allowed)
 def execute(self,config,action,message): self.executed.append(action['action_type']); return {'message_id':900}

def message(event=1,text='spam',user=200): return {'update_id':event,'message':{'message_id':event,'chat':{'id':-100888,'type':'supergroup'},'from':{'id':user,'is_bot':False},'text':text}}

def rule(a,code='spam',action='delete',kind='keyword',config=None):
 with transaction(a['tenant']) as db:
  db.execute('INSERT INTO tgmate.moderation_rules(tenant_id,group_config_id,rule_code,name,rule_type,match_config,action) VALUES(%s,%s,%s,%s,%s,%s,%s)',(a['tenant'],a['group'],code,code,kind,Jsonb(config or {'words':['spam']}),action))

def work(a,adapter=None): return run_once(a['tenant'],settings()['worker_id'],adapter or Adapter())

def test_rule_evaluation_and_delete(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message())
 assert work(a)['matched']==1
 adapter=Adapter(); assert work(a,adapter)['status']=='succeeded'
 assert adapter.executed==['delete_message']
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==1

def test_multiple_rules_count_once(moderation):
 a=moderation; rule(a,code='one'); rule(a,code='two'); receive(a['tenant'],a['group'],message())
 assert work(a)['matched']==2
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==1
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_actions').fetchone()['n']==1

def test_webhook_duplicate_not_counted_twice(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message())
 assert receive(a['tenant'],a['group'],message())['status']=='duplicate'
 work(a)
 with transaction(a['tenant']) as db: assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==1

def test_admin_exempt_before_violation(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message())
 assert work(a,Adapter(role='administrator'))['status']=='exempt'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==0

def test_whitelist_exempt(moderation):
 a=moderation; rule(a)
 with transaction(a['tenant']) as db: db.execute('INSERT INTO tgmate.moderation_exemptions(tenant_id,group_config_id,telegram_user_id) VALUES(%s,%s,200)',(a['tenant'],a['group']))
 receive(a['tenant'],a['group'],message()); assert work(a)['status']=='exempt'

def test_no_permission_no_delete(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 adapter=Adapter(allowed=False); assert work(a,adapter)['status']=='blocked'; assert not adapter.executed

def test_ban_defaults_to_human_review(moderation):
 a=moderation; rule(a,action='ban'); receive(a['tenant'],a['group'],message()); work(a)
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_actions').fetchone()['n']==0
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_review_queue').fetchone()['n']==1

def test_window_resets_without_deleting_history(moderation):
 a=moderation; rule(a,action='log'); receive(a['tenant'],a['group'],message()); work(a); work(a)
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.moderation_violation_counters SET window_started_at=now()-interval '2 days',window_ends_at=now()-interval '1 day'")
 receive(a['tenant'],a['group'],message(2)); work(a)
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==1
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==2

def test_promoted_admin_protected_at_execution(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 adapter=Adapter(role='administrator'); assert work(a,adapter)['status']=='exempt'; assert not adapter.executed

def test_unsupported_legacy_rule_goes_to_review(moderation):
 a=moderation; rule(a,kind='custom'); receive(a['tenant'],a['group'],message()); work(a)
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==0
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_review_queue').fetchone()['n']==1

def test_threshold_default_review_and_not_auto_ban(moderation):
 a=moderation; rule(a,action='log')
 for i in range(1,4): receive(a['tenant'],a['group'],message(i)); work(a); work(a)
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT violation_count FROM tgmate.moderation_violation_counters').fetchone()['violation_count']==3
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_review_queue').fetchone()['n']==1

def test_unknown_action_is_manual_and_not_retried(moderation):
 from app.worker import UnknownSend
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 class Broken(Adapter):
  def execute(self,*args): raise UnknownSend()
 assert work(a,Broken())['status']=='manual_review'
 assert work(a)['status']=='idle'
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT reserved_value FROM tgmate.quota_accounts WHERE entitlement_code='moderation.actions'").fetchone()['reserved_value']==1

def test_manual_resolution_consumes_once(moderation):
 from app.worker import UnknownSend
 from app.moderation import resolve_action
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 class Broken(Adapter):
  def execute(self,*args): raise UnknownSend()
 result=work(a,Broken())
 resolve_action(a['tenant'],result['action_id'],a['user'],'confirm_succeeded','管理员已核对群组记录确认消息确实删除成功')
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='moderation.actions'").fetchone()['consumed_value']==1

def test_join_evaluation_queues_welcome_without_claiming_delivery(moderation):
 a=moderation; update=message(); update['message']['new_chat_members']=[{'id':300,'is_bot':False},{'id':301,'is_bot':False}]
 receive(a['tenant'],a['group'],update)
 assert work(a)['status']=='welcome_queued'; assert work(a)['status']=='welcome_queued'
 with transaction(a['tenant']) as db:
  users=[r['telegram_user_id'] for r in db.execute('SELECT telegram_user_id FROM tgmate.moderation_welcome_deliveries').fetchall()]
  assert set(users)=={300,301}
  assert db.execute("SELECT count(*) AS n FROM tgmate.moderation_welcome_deliveries WHERE welcome_status='pending'").fetchone()['n']==2

def test_duplicate_rule_counts_original_time_not_future_queued_events(moderation):
 a=moderation; rule(a,action='log',kind='duplicate',config={'threshold':2,'window_seconds':10})
 receive(a['tenant'],a['group'],message(1)); receive(a['tenant'],a['group'],message(2))
 assert work(a)['matched']==0
 assert work(a)['matched']==1

def test_unknown_member_identity_never_punished(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message())
 assert work(a,Adapter(role='unknown'))['status']=='manual_review'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==0

def test_manual_ban_review_requires_real_tenant_member(moderation):
 from app.moderation import review
 from app.relay import Rejected
 a=moderation; rule(a,action='ban'); receive(a['tenant'],a['group'],message()); work(a)
 with transaction(a['tenant']) as db: item=db.execute('SELECT id FROM tgmate.moderation_review_queue').fetchone()['id']
 with pytest.raises(Rejected): review(a['tenant'],item,uuid.uuid4(),'ban','管理员批准封禁但必须验证其租户成员身份')
 review(a['tenant'],item,a['user'],'ban','管理员人工审核后明确批准对该用户执行封禁')
 adapter=Adapter(); assert work(a,adapter)['status']=='succeeded'; assert adapter.executed==['ban']

def test_webhook_group_binding_and_secret(moderation):
 import json,os
 from pathlib import Path
 from fastapi.testclient import TestClient
 from app.main import app
 a=moderation; p=Path(os.environ['TGMATE_CONFIG']); c=json.loads(p.read_text()); c['moderation_webhooks']={str(a['group']):{'tenant_id':str(a['tenant']),'secret':'test-mod-secret'}}; p.write_text(json.dumps(c))
 client=TestClient(app); url=f"/webhooks/moderation/{a['group']}"
 assert client.post(url,json=message()).status_code==403
 wrong=message(); wrong['message']['chat']['id']=-100777
 assert client.post(url,json=wrong,headers={'X-Telegram-Bot-Api-Secret-Token':'test-mod-secret'}).status_code==422
 assert client.post(url,json=message(),headers={'X-Telegram-Bot-Api-Secret-Token':'test-mod-secret'}).json()['status']=='queued'

def test_rate_limited_action_retries_without_double_charging(moderation):
 from app.worker import RetryLater
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 class Limited(Adapter):
  def execute(self,*args): raise RetryLater(60)
 assert work(a,Limited())['status']=='pending'
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.outbox_events SET available_at=now() WHERE event_type='moderation.action'")
 assert work(a)['status']=='succeeded'
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='moderation.actions'").fetchone()['consumed_value']==1
  assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_action_attempts').fetchone()['n']==2

def test_retry_known_failure_has_new_reservation(moderation):
 from app.worker import PermanentFailure
 from app.moderation import resolve_action
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 class Broken(Adapter):
  def execute(self,*args): raise PermanentFailure()
 result=work(a,Broken()); assert result['status']=='failed'
 resolve_action(a['tenant'],result['action_id'],a['user'],'retry','管理员已确认原动作没有执行并修复权限后批准重试')
 assert work(a)['status']=='succeeded'

def test_expired_action_lease_is_unknown_not_repeated(moderation):
 a=moderation; rule(a); receive(a['tenant'],a['group'],message()); work(a)
 class Expires(Adapter):
  def execute(self,*args):
   with transaction(a['tenant']) as db:
    db.execute("UPDATE tgmate.task_leases SET leased_at=now()-interval '2 minutes',lease_until=now()-interval '1 minute'")
    db.execute("UPDATE tgmate.outbox_events SET locked_until=now()-interval '1 minute' WHERE event_type='moderation.action'")
   return {'ok':True}
 assert work(a,Expires())['status']=='fenced'
 assert work(a)['status']=='idle'
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.moderation_actions').fetchone()['status']=='manual_review'
  assert db.execute('SELECT status FROM tgmate.moderation_action_attempts').fetchone()['status']=='unknown'

def test_member_check_rate_limit_retries_without_punishment(moderation):
 from app.worker import RetryLater
 a=moderation; rule(a); receive(a['tenant'],a['group'],message())
 class Limited(Adapter):
  def member_role(self,*args): raise RetryLater(60)
 assert work(a,Limited())['status']=='retry_wait'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_violations').fetchone()['n']==0

@pytest.mark.parametrize('kind,config,text,expected',[
 ('keyword',{'words':['SPAM']},'some spam',True),
 ('link',{},'https://example.test',True),
 ('invite_link',{},'https://t.me/+invite',True),
 ('mention_spam',{'threshold':2},'@one @two',True),
 ('keyword',{'words':['spam']},'ordinary message',False),
 ('regex',{'pattern':'^hello$'},'hello',True),
])
def test_rule_contract(kind,config,text,expected):
 from app.moderation import rule_match
 assert rule_match({'rule_type':kind,'match_config':config},{'text':text},[]) is expected

def test_invalid_binding_cannot_prevent_control_only_disable(moderation):
 a=moderation
 with psycopg.connect(**DB) as db: db.execute("UPDATE tgmate.executor_bindings SET status='invalid' WHERE robot_instance_id=%s",(a['modrobot'],))
 with transaction(a['tenant']) as db: assert db.execute('UPDATE tgmate.moderation_group_configs SET enabled=false WHERE id=%s RETURNING enabled',(a['group'],)).fetchone()['enabled'] is False
 with transaction(a['tenant']) as db: assert db.execute('UPDATE tgmate.moderation_group_configs SET enabled=false WHERE id=%s RETURNING enabled',(a['group'],)).fetchone()['enabled'] is False
 with pytest.raises(psycopg.Error):
  with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.moderation_group_configs SET enabled=true WHERE id=%s',(a['group'],))


def test_disable_exception_cannot_change_policy_or_identity(moderation):
 a=moderation
 with psycopg.connect(**DB) as db: db.execute("UPDATE tgmate.executor_bindings SET status='invalid' WHERE robot_instance_id=%s",(a['modrobot'],))
 with pytest.raises(psycopg.Error):
  with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.moderation_group_configs SET enabled=false,violation_threshold=9 WHERE id=%s',(a['group'],))
