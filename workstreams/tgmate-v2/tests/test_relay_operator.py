import uuid,pytest,psycopg
from conftest import DB,seed
from test_service import service,message,post,Adapter
from app.relay_operator import challenge,consume,authorized
from app.db import transaction
from app.worker import run_once
from app.relay import Rejected

def command_update(command,user=100,event=999):
 result=message(event,user);result['message']['text']=command;return result

def test_challenge_identity_bound_expiry_and_confirmation(service):
 _,a,_=service;result=challenge(a['tenant'],a['config'],a['user'])
 with transaction(a['tenant']) as db:
  config=db.execute('SELECT * FROM tgmate.relay_configs WHERE id=%s',(a['config'],)).fetchone()
  assert not authorized(db,config)
  row=db.execute('SELECT * FROM tgmate.relay_operator_authorizations WHERE config_id=%s',(a['config'],)).fetchone()
  import hashlib
  code=result['command'][len('/start relayop_'):]
  assert row['challenge_hash']==hashlib.sha256(code.encode()).hexdigest() and code not in row['challenge_hash']
 assert consume(a['tenant'],a['bot'],command_update(result['command'],200))['status']=='operator_authorization_rejected'
 assert consume(a['tenant'],uuid.uuid4(),command_update(result['command']))['status']=='operator_authorization_rejected'
 assert consume(a['tenant'],a['bot'],command_update(result['command']))['status']=='operator_authorized'
 assert consume(a['tenant'],a['bot'],command_update(result['command']))['status']=='operator_already_authorized'
 with transaction(a['tenant']) as db:
  assert authorized(db,config)
  assert db.execute("SELECT count(*) AS n FROM tgmate.outbox_events WHERE event_type='bot.private_reply'").fetchone()['n']==1

def test_intake_and_worker_wait_until_operator_accepts(service):
 client,a,c=service; assert post(client,a,message()).status_code==200
 result=challenge(a['tenant'],a['config'],a['user'])
 assert post(client,a,message(2)) .status_code==422
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='waiting_for_operator_authorization'
 assert consume(a['tenant'],a['bot'],command_update(result['command']))['status']=='operator_authorized'
 assert run_once(a['tenant'],c['worker_id'],Adapter())['status']=='succeeded'

def test_expired_or_replaced_challenge_cannot_confirm(service):
 _,a,_=service;old=challenge(a['tenant'],a['config'],a['user']);new=challenge(a['tenant'],a['config'],a['user'])
 assert consume(a['tenant'],a['bot'],command_update(old['command']))['status']=='operator_authorization_rejected'
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.relay_operator_authorizations SET expires_at=now()-interval '1 second' WHERE config_id=%s",(a['config'],))
 assert consume(a['tenant'],a['bot'],command_update(new['command']))['status']=='operator_authorization_expired'

def test_operator_change_invalidates_authorization_and_wrong_tenant(service):
 _,a,_=service
 with transaction(a['tenant']) as db:
  db.execute('UPDATE tgmate.relay_configs SET operator_user_id=300 WHERE id=%s',(a['config'],))
  config=db.execute('SELECT * FROM tgmate.relay_configs WHERE id=%s',(a['config'],)).fetchone()
  assert not authorized(db,config)
 with psycopg.connect(**DB) as db:other=seed(db)
 with pytest.raises(Rejected): challenge(other['tenant'],a['config'],other['user'])

def test_gateway_consumes_authorization_while_relay_paused(service):
 from test_bot_gateway import gateway
 _,a,_=service;result=challenge(a['tenant'],a['config'],a['user'])
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.relay_configs SET status='paused' WHERE id=%s",(a['config'],))
 client,url,headers=gateway(a,a['bot'])
 response=client.post(url,json=command_update(result['command']),headers=headers)
 assert response.status_code==200 and response.json()['status']=='operator_authorized'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.relay_operations').fetchone()['n']==0

def test_changing_operator_away_and_back_requires_new_confirmation(service):
 _,a,_=service
 with transaction(a['tenant']) as db:
  db.execute('UPDATE tgmate.relay_configs SET operator_user_id=300 WHERE id=%s',(a['config'],))
  db.execute('UPDATE tgmate.relay_configs SET operator_user_id=100 WHERE id=%s',(a['config'],))
  config=db.execute('SELECT * FROM tgmate.relay_configs WHERE id=%s',(a['config'],)).fetchone()
  assert not authorized(db,config)
 result=challenge(a['tenant'],a['config'],a['user'])
 assert consume(a['tenant'],a['bot'],command_update(result['command']))['status']=='operator_authorized'
