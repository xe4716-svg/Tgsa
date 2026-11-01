import uuid
import pytest
import psycopg
from conftest import DB
from test_service import service
from app.broadcast import create_plan,preview
from app.relay import Rejected

@pytest.fixture
def broadcast(service):
 cl,a,c=service
 r,b,e,d,i,s=[uuid.uuid4() for _ in range(6)]
 with psycopg.connect(**DB) as db:
  db.execute("INSERT INTO tgmate.robot_instances(id,tenant_id,product_type_code,name) VALUES(%s,%s,'broadcast','broadcast test')",(r,a['tenant']))
  db.execute("INSERT INTO tgmate.telegram_bots(id,tenant_id,token_vault_ref,token_fingerprint,status) VALUES(%s,%s,'test-only',%s,'active')",(b,a['tenant'],str(b)))
  db.execute("INSERT INTO tgmate.executor_bindings(id,tenant_id,robot_instance_id,telegram_bot_id,purpose) VALUES(%s,%s,%s,%s,'send')",(e,a['tenant'],r,b))
  db.execute("INSERT INTO tgmate.telegram_destinations(id,tenant_id,telegram_peer_id,destination_type,title) VALUES(%s,%s,-100999,'supergroup','test target')",(d,a['tenant']))
  db.execute('INSERT INTO tgmate.telegram_identities(id,tenant_id,telegram_user_id) VALUES(%s,%s,999)',(i,a['tenant']))
  db.execute("INSERT INTO tgmate.telegram_sessions(id,tenant_id,telegram_identity_id,session_vault_ref,session_fingerprint,authorization_status) VALUES(%s,%s,%s,'test-only',%s,'authorized')",(s,a['tenant'],i,str(s)))
 return a,dict(request_key='plan-1',module='bot_broadcast',text='test',targets=[d],robot=r,binding=e),s

def test_bot_plan_preview_and_idempotence(broadcast):
 a,args,s=broadcast
 result=create_plan(a['tenant'],**args)
 assert not result['duplicate']
 assert create_plan(a['tenant'],**args)['duplicate']
 p=preview(a['tenant'],result['task_id'])
 assert p['task']['business_module']=='bot_broadcast'
 assert p['content']=={'text':'test'} and len(p['targets'])==1
 assert not p['send_ready']

def test_same_key_cannot_change_content(broadcast):
 a,args,s=broadcast; create_plan(a['tenant'],**args)
 with pytest.raises(Rejected): create_plan(a['tenant'],**dict(args,text='changed'))

def test_account_channel_has_no_robot(broadcast):
 a,args,s=broadcast
 args=dict(args,module='account_broadcast',robot=None,binding=None,session=s)
 result=create_plan(a['tenant'],**args)
 assert preview(a['tenant'],result['task_id'])['task']['business_module']=='account_broadcast'
 with psycopg.connect(**DB) as db:
  assert db.execute('SELECT robot_instance_id FROM tgmate.task_registry WHERE id=%s',(result['task_id'],)).fetchone()[0] is None

def test_account_channel_rejects_robot(broadcast):
 a,args,s=broadcast
 with pytest.raises(Rejected): create_plan(a['tenant'],**dict(args,module='account_broadcast',session=s))

def test_foreign_target_rejected(broadcast):
 a,args,s=broadcast
 with pytest.raises(Rejected): create_plan(a['tenant'],**dict(args,targets=[uuid.uuid4()]))

def test_wrong_product_binding_rejected(broadcast):
 a,args,s=broadcast
 with pytest.raises(Rejected): create_plan(a['tenant'],**dict(args,robot=a['robot'],binding=a['binding']))
