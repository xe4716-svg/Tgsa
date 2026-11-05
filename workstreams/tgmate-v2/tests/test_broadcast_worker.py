from test_service import service
import uuid
import psycopg
from conftest import DB
import threading
from test_broadcast import broadcast
from app.broadcast import create_plan,start_run,change_run
from app.broadcast_worker import run_once
from app.db import transaction,settings
from app.worker import UnknownSend,RetryLater

class Adapter:
 def __init__(self,allowed=True): self.sent=[]; self.allowed=allowed; self.checked=[]
 def preflight(self,module,executor,destination): self.checked.append(module); return {'can_send_messages':self.allowed}
 def send(self,module,executor,destination,content): self.sent.append((module,destination['telegram_peer_id'],content)); return 501

def prepare(broadcast,account=False):
 a,args,session=broadcast
 if account: args=dict(args,module='account_broadcast',robot=None,binding=None,session=session)
 with transaction(a['tenant']) as db:
  db.execute("INSERT INTO tgmate.quota_accounts(tenant_id,entitlement_code,period_start,period_end,limit_value) VALUES(%s,'broadcast.messages',now()-interval '1 hour',now()+interval '30 days',100)",(a['tenant'],))
 task=create_plan(a['tenant'],**args)['task_id']
 run=start_run(a['tenant'],task)['run_id']
 return a,task,run

def test_bot_dispatch_preflight_and_settlement(broadcast):
 a,task,run=prepare(broadcast); adapter=Adapter()
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='succeeded'
 assert adapter.checked==['bot_broadcast'] and len(adapter.sent)==1
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s',(run,)).fetchone()['status']=='completed'
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['consumed_value']==1

 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='idle'

def test_bot_worker_does_not_claim_account_broadcast_tasks(broadcast):
 a,task,run=prepare(broadcast,account=True); adapter=Adapter()
 assert run_once(a['tenant'],settings()['worker_id'],adapter,business_module='bot_broadcast')['status']=='idle'
 assert adapter.checked==[] and adapter.sent==[]
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s',(run,)).fetchone()['status']=='queued'

def test_account_dispatch_stays_independent(broadcast):
 a,task,run=prepare(broadcast,account=True); adapter=Adapter()
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='succeeded'
 assert adapter.checked==['account_broadcast']
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.bot_broadcast_deliveries').fetchone()['n']==0
  assert db.execute('SELECT count(*) AS n FROM tgmate.account_broadcast_deliveries').fetchone()['n']==1

def test_denied_preflight_does_not_send_or_charge(broadcast):
 a,task,run=prepare(broadcast); adapter=Adapter(False)
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='blocked'
 assert not adapter.sent
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.quota_reservations').fetchone()['n']==0

def test_unknown_send_is_not_repeated(broadcast):
 a,task,run=prepare(broadcast)
 class Broken(Adapter):
  def send(self,*args): raise UnknownSend()
 assert run_once(a['tenant'],settings()['worker_id'],Broken())['status']=='unknown'
 assert run_once(a['tenant'],settings()['worker_id'],Adapter())['status']=='idle'
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT reserved_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['reserved_value']==1

def test_pause_resume_preserves_queue(broadcast):
 a,task,run=prepare(broadcast); adapter=Adapter()
 change_run(a['tenant'],run,'pause')
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='idle'
 change_run(a['tenant'],run,'resume')
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='succeeded'

def test_cancel_before_send(broadcast):
 a,task,run=prepare(broadcast); adapter=Adapter()
 change_run(a['tenant'],run,'cancel')
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='idle'
 assert not adapter.sent

def test_rate_limit_schedules_without_rotation(broadcast):
 a,task,run=prepare(broadcast)
 class Limited(Adapter):
  def send(self,*args): raise RetryLater(30)
 assert run_once(a['tenant'],settings()['worker_id'],Limited())['status']=='pending'
 assert run_once(a['tenant'],settings()['worker_id'],Adapter())['status']=='idle'

def test_pause_during_preflight_prevents_network_send(broadcast):
 a,task,run=prepare(broadcast)
 class Pauses(Adapter):
  def preflight(self,*args): change_run(a['tenant'],run,'pause'); return {'can_send_messages':True}
 adapter=Pauses()
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='paused'
 assert not adapter.sent


def test_cancel_releases_rate_limited_reservation(broadcast):
 a,task,run=prepare(broadcast)
 class Limited(Adapter):
  def send(self,*args): raise RetryLater(30)
 run_once(a['tenant'],settings()['worker_id'],Limited())
 change_run(a['tenant'],run,'cancel')
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT reserved_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['reserved_value']==0

def test_unknown_requires_manual_evidence_and_settlement(broadcast):
 import pytest
 from app.relay import Rejected
 from app.broadcast import review_target
 a,task,run=prepare(broadcast)
 class Broken(Adapter):
  def send(self,*args): raise UnknownSend()
 run_once(a['tenant'],settings()['worker_id'],Broken())
 destination=broadcast[1]['targets'][0]
 with pytest.raises(Rejected): review_target(a['tenant'],run,destination,'retry','已经检查但无法确定是否发送成功')
 review_target(a['tenant'],run,destination,'confirm_sent','人工查看目标群组确认该消息已经成功发送',900)
 assert change_run(a['tenant'],run,'resume')['status']=='completed'
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['consumed_value']==1

def test_manual_retry_known_failure_gets_new_reservation(broadcast):
 from app.broadcast import review_target
 from app.worker import PermanentFailure
 a,task,run=prepare(broadcast)
 class Broken(Adapter):
  def send(self,*args): raise PermanentFailure()
 assert run_once(a['tenant'],settings()['worker_id'],Broken())['status']=='failed'
 review_target(a['tenant'],run,broadcast[1]['targets'][0],'retry','管理员已恢复群组权限并批准重新发送')
 change_run(a['tenant'],run,'resume')
 assert run_once(a['tenant'],settings()['worker_id'],Adapter())['status']=='succeeded'

def test_two_targets_have_distinct_quota_reservations(broadcast):
 import uuid,psycopg
 from conftest import DB
 a,args,session=broadcast; second=uuid.uuid4()
 with psycopg.connect(**DB) as db: db.execute("INSERT INTO tgmate.telegram_destinations(id,tenant_id,telegram_peer_id,destination_type,title) VALUES(%s,%s,-100998,'supergroup','second target')",(second,a['tenant']))
 args['targets'].append(second)
 a,task,run=prepare(broadcast); adapter=Adapter()
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='succeeded'
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='succeeded'
 assert len(adapter.sent)==2
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['consumed_value']==2
  assert db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s',(run,)).fetchone()['status']=='completed'

def test_scheduled_run_not_sent_early(broadcast):
 from datetime import datetime,timedelta,timezone
 a,args,session=broadcast
 args['scheduled_at']=datetime.now(timezone.utc)+timedelta(hours=1)
 a,task,run=prepare(broadcast); adapter=Adapter()
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='idle'
 assert not adapter.sent

def test_cancel_inflight_unknown_can_still_be_reconciled(broadcast):
 from app.broadcast import review_target
 a,task,run=prepare(broadcast)
 class Cancels(Adapter):
  def send(self,*args): change_run(a['tenant'],run,'cancel'); raise UnknownSend()
 assert run_once(a['tenant'],settings()['worker_id'],Cancels())['status']=='unknown'
 result=review_target(a['tenant'],run,broadcast[1]['targets'][0],'confirm_sent','取消时已在途，人工确认目标群中已收到该消息',999)
 assert result['status']=='cancelled'
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['consumed_value']==1

def test_expired_broadcast_lease_never_blindly_resends(broadcast):
 a,task,run=prepare(broadcast)
 class Expired(Adapter):
  def send(self,*args):
   with transaction(a['tenant']) as db:
    db.execute("UPDATE tgmate.task_leases SET leased_at=now()-interval '2 minutes',lease_until=now()-interval '1 minute'")
    db.execute("UPDATE tgmate.outbox_events SET locked_until=now()-interval '1 minute' WHERE event_type='broadcast.dispatch'")
   return 77
 assert run_once(a['tenant'],settings()['worker_id'],Expired())['status']=='fenced'
 adapter=Adapter()
 assert run_once(a['tenant'],settings()['worker_id'],adapter)['status']=='idle'
 assert not adapter.sent
 with transaction(a['tenant']) as db: assert db.execute('SELECT status FROM tgmate.bot_broadcast_deliveries').fetchone()['status']=='unknown'

def test_permission_revoked_after_rate_limit_releases_quota(broadcast):
 a,task,run=prepare(broadcast)
 class Limited(Adapter):
  def send(self,*args): raise RetryLater(30)
 run_once(a['tenant'],settings()['worker_id'],Limited())
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.outbox_events SET available_at=now() WHERE event_type='broadcast.dispatch'")
 assert run_once(a['tenant'],settings()['worker_id'],Adapter(False))['status']=='blocked'
 with transaction(a['tenant']) as db:
  assert db.execute("SELECT reserved_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['reserved_value']==0


def test_two_workers_cannot_claim_same_dispatch_during_preflight(broadcast):
 a,task,run=prepare(broadcast)
 worker1=settings()['worker_id']
 worker2=str(uuid.uuid4())
 with psycopg.connect(**DB) as db:
  db.execute("INSERT INTO tgmate.worker_nodes(id,worker_code,worker_type,status) VALUES(%s,%s,'relay','healthy')",(worker2,'outbox-concurrency-'+worker2))
 entered=threading.Event(); resume=threading.Event()
 class BlockingAdapter(Adapter):
  def preflight(self,module,executor,destination):
   self.checked.append(module)
   entered.set()
   if not resume.wait(10): raise RuntimeError('preflight barrier timed out')
   return {'can_send_messages':True}
 first_adapter=BlockingAdapter()
 second_adapter=Adapter()
 result={}; errors=[]
 def first_worker():
  try:
   result['first']=run_once(a['tenant'],worker1,first_adapter)
  except BaseException as exc:
   errors.append(exc)
 thread=threading.Thread(target=first_worker,daemon=True)
 thread.start()
 try:
  assert entered.wait(5), f'first worker did not reach external preflight: {errors!r}'
  with transaction(a['tenant']) as db:
   state=db.execute("SELECT status,payload->>'phase' AS phase FROM tgmate.outbox_events WHERE aggregate_id=%s AND event_type='broadcast.dispatch'",(run,)).fetchone()
  assert state['status']=='publishing' and state['phase']=='preflight'
  second=run_once(a['tenant'],worker2,second_adapter)
 finally:
  resume.set()
  thread.join(10)
  with psycopg.connect(**DB) as db:
   db.execute("DELETE FROM tgmate.worker_nodes WHERE id=%s",(worker2,))
 assert not thread.is_alive(), 'first worker did not finish'
 assert not errors
 assert result['first']['status']=='succeeded'
 assert second['status']=='idle'
 assert first_adapter.checked==['bot_broadcast']
 assert len(first_adapter.sent)==1
 assert second_adapter.checked==[] and second_adapter.sent==[]
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.broadcast_task_runs WHERE id=%s',(run,)).fetchone()['status']=='completed'
  assert db.execute("SELECT status FROM tgmate.outbox_events WHERE aggregate_id=%s AND event_type='broadcast.dispatch'",(run,)).fetchone()['status']=='published'
  assert db.execute("SELECT consumed_value FROM tgmate.quota_accounts WHERE entitlement_code='broadcast.messages'").fetchone()['consumed_value']==1
