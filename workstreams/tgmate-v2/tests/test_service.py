import json,uuid,os
import pytest
import psycopg
from fastapi.testclient import TestClient
from app.main import app
from app.db import transaction
from app.worker import run_once,UnknownSend,RetryLater
from conftest import ROOT,DB,seed

@pytest.fixture
def service(tmp_path,monkeypatch):
 with psycopg.connect(**DB) as db:
  a=seed(db)
 c=json.loads(__import__('pathlib').Path(os.environ.get('TGMATE_TEST_APP_CONFIG',ROOT/'.local/app.json')).read_text())
 c['api_keys']={'test-admin-key':{'tenant_id':str(a['tenant']),'role':'admin','user_id':str(a['user'])}}
 c['allowed_hosts']=['testserver','127.0.0.1','localhost','::1']
 c['webhooks']={str(a['config']):{'tenant_id':str(a['tenant']),'secret':'test-webhook-secret'}}
 p=tmp_path/'app.json'; p.write_text(json.dumps(c)); monkeypatch.setenv('TGMATE_CONFIG',str(p))
 return TestClient(app),a,{'worker_id':c['worker_id'],'mode':c['mode']}

def message(event=1,user=200,reply=None):
 m={'message_id':event,'from':{'id':user,'is_bot':False},'chat':{'id':user,'type':'private'},'text':'test customer content'}
 if reply: m['reply_to_message']={'message_id':reply}
 return {'update_id':event,'message':m}

def post(client,a,data,secret='test-webhook-secret'):
 return client.post(f"/webhooks/telegram/{a['config']}",json=data,headers={'X-Telegram-Bot-Api-Secret-Token':secret})

class Adapter:
 def __init__(self): self.targets=[]
 def send(self,target,payload,config): self.targets.append(target); return 700+len(self.targets)

def test_webhook_requires_secret(service):
 cl,a,c=service
 assert post(cl,a,message(),secret='wrong').status_code==403

def test_real_database_roundtrip(service):
 cl,a,c=service; adapter=Adapter()
 assert post(cl,a,message()).json()['status']=='queued'
 assert run_once(a['tenant'],c['worker_id'],adapter)['status']=='succeeded'
 assert post(cl,a,message(2,100,701)).json()['status']=='queued'
 assert run_once(a['tenant'],c['worker_id'],adapter)['status']=='succeeded'
 assert adapter.targets==[100,200]
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.audit_logs').fetchone()['n']>=4
  payload=db.execute('SELECT ciphertext FROM tgmate.relay_payloads LIMIT 1').fetchone()['ciphertext']
  assert 'source_chat_id' not in payload

def test_duplicate_is_atomic(service):
 cl,a,c=service
 assert post(cl,a,message()).json()['status']=='queued'
 assert post(cl,a,message()).json()['status']=='duplicate'
 with transaction(a['tenant']) as db:
  for table in ['inbox_events','relay_operations','outbox_events','task_registry']:
   from psycopg import sql
   assert db.execute(sql.SQL('SELECT count(*) AS n FROM tgmate.{}').format(sql.Identifier(table))).fetchone()['n']==1

def test_operator_cannot_pick_arbitrary_customer(service):
 cl,a,c=service
 assert post(cl,a,message(2,100,999)).status_code==422
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.outbox_events').fetchone()['n']==0

def test_worker_marks_uncertain(service):
 cl,a,c=service; post(cl,a,message())
 class Broken:
  def send(self,*args): raise UnknownSend()
 assert run_once(a['tenant'],c['worker_id'],Broken())['status']=='uncertain'
 assert run_once(a['tenant'],c['worker_id'],Broken())['status']=='idle'

def test_pause_cancels_queued(service):
 cl,a,c=service; post(cl,a,message())
 response=cl.post(f"/api/relay/configs/{a['config']}/state",json={'status':'paused'},headers={'Authorization':'Bearer test-admin-key'})
 assert response.status_code==200
 assert run_once(a['tenant'],c['worker_id'])['status']=='cancelled'

def test_api_auth_and_tenant_hiding(service):
 cl,a,c=service
 assert cl.get('/api/relay/configs').status_code==401
 r=cl.get('/api/relay/configs',headers={'Authorization':'Bearer test-admin-key'})
 assert r.status_code==200 and len(r.json())==1
 assert r.json()[0]['id']==str(a['config'])

def test_rate_limit_schedules_retry(service):
 cl,a,c=service; post(cl,a,message())
 class Limited:
  def send(self,*args): raise RetryLater(60)
 assert run_once(a['tenant'],c['worker_id'],Limited())['status']=='retry_wait'
 assert run_once(a['tenant'],c['worker_id'])['status']=='idle'

def test_worker_blocks_old_reply_after_unblock(service):
 cl,a,c=service; post(cl,a,message()); run_once(a['tenant'],c['worker_id'],Adapter())
 with transaction(a['tenant']) as db:
  db.execute("UPDATE tgmate.relay_conversations SET status='blocked'")
  db.execute("UPDATE tgmate.relay_conversations SET status='open'")
 assert post(cl,a,message(2,100,701)).status_code==422

def test_quota_consumed_once(service):
 cl,a,c=service; post(cl,a,message()); post(cl,a,message())
 with transaction(a['tenant']) as db:
  q=db.execute('SELECT reserved_value,consumed_value FROM tgmate.quota_accounts').fetchone()
  assert q=={'reserved_value':1,'consumed_value':0}
 run_once(a['tenant'],c['worker_id'],Adapter())
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT reserved_value,consumed_value FROM tgmate.quota_accounts').fetchone()=={'reserved_value':0,'consumed_value':1}

def test_quota_exhaustion_rolls_back_inbox_and_task(service):
 cl,a,c=service
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.quota_accounts SET limit_value=0')
 assert post(cl,a,message()).status_code==422
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.inbox_events').fetchone()['n']==0
  assert db.execute('SELECT count(*) AS n FROM tgmate.task_registry').fetchone()['n']==0

def test_uncertain_retains_quota_until_manual_resolution(service):
 cl,a,c=service; result=post(cl,a,message()).json()
 class Broken:
  def send(self,*args): raise UnknownSend()
 run_once(a['tenant'],c['worker_id'],Broken())
 with transaction(a['tenant']) as db: assert db.execute('SELECT reserved_value FROM tgmate.quota_accounts').fetchone()['reserved_value']==1
 url=f"/api/relay/operations/{result['operation_id']}/resolve"
 r=cl.post(url,json={'status':'succeeded','target_message_id':777,'evidence':'人工检查目标聊天后确认该条消息已经送达'},headers={'Authorization':'Bearer test-admin-key'})
 assert r.status_code==200
 with transaction(a['tenant']) as db: assert db.execute('SELECT consumed_value FROM tgmate.quota_accounts').fetchone()['consumed_value']==1
 assert cl.post(url,json={'status':'failed','evidence':'重复提交应被拒绝避免重复结算'},headers={'Authorization':'Bearer test-admin-key'}).status_code==409

def test_malformed_webhook_rejected(service):
 cl,a,c=service
 for data in [[],{'message':{'from':[],'chat':{}},'update_id':1},message(-1),{'update_id':True,'message':message()['message']}]:
  assert post(cl,a,data).status_code==422

def test_revoked_config_cannot_reactivate(service):
 cl,a,c=service; url=f"/api/relay/configs/{a['config']}/state"; auth={'Authorization':'Bearer test-admin-key'}
 assert cl.post(url,json={'status':'revoked'},headers=auth).status_code==200
 assert cl.post(url,json={'status':'active'},headers=auth).status_code==409

def test_expired_lease_fences_result_and_recovers_unknown(service):
 cl,a,c=service; post(cl,a,message())
 class Expired:
  def send(self,*args):
   with transaction(a['tenant']) as db:
    db.execute("UPDATE tgmate.task_leases SET leased_at=now()-interval '2 minutes',lease_until=now()-interval '1 minute'")
    db.execute("UPDATE tgmate.outbox_events SET locked_until=now()-interval '1 minute'")
   return 991
 assert run_once(a['tenant'],c['worker_id'],Expired())['status']=='fenced'
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='idle'
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT status FROM tgmate.relay_operations').fetchone()['status']=='uncertain'
  assert db.execute('SELECT status FROM tgmate.delivery_attempts').fetchone()['status']=='unknown'
  assert db.execute('SELECT reserved_value FROM tgmate.quota_accounts').fetchone()['reserved_value']==1
