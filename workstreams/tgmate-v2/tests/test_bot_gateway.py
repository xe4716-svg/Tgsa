import json,os,uuid,psycopg,pytest
from pathlib import Path
from fastapi.testclient import TestClient
from conftest import DB
from test_service import service
from test_moderation import moderation,message
from test_verification import VerificationAdapter,join,notice,callback_update
from app.main import app
from app.bot_gateway import dispatch
from app.db import transaction
from app.relay import Rejected


def gateway(a,bot):
 p=Path(os.environ['TGMATE_CONFIG']); c=json.loads(p.read_text()); c['bot_webhooks']={str(bot):{'tenant_id':str(a['tenant']),'secret':'test-bot-gateway'}}; p.write_text(json.dumps(c))
 return TestClient(app),f'/webhooks/bots/{bot}',{'X-Telegram-Bot-Api-Secret-Token':'test-bot-gateway'}


def test_gateway_auth_and_group_routing(moderation):
 a=moderation; client,url,headers=gateway(a,a['modbot'])
 assert client.post(url,json=message()).status_code==403
 response=client.post(url,json=message(),headers=headers)
 assert response.status_code==200 and response.json()['status']=='queued'
 assert client.post(url,json=message(),headers=headers).json()['status']=='duplicate'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_message_events').fetchone()['n']==1


def test_gateway_never_routes_other_bots_group(moderation):
 a=moderation; client,url,headers=gateway(a,a['bot'])
 assert client.post(url,json=message(),headers=headers).json()['status']=='ignored_unconfigured_group'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.moderation_message_events').fetchone()['n']==0


def test_gateway_private_message_routes_to_relay(service):
 cl,a,c=service; client,url,headers=gateway(a,a['bot'])
 from test_service import message as private
 assert client.post(url,json=private(),headers=headers).json()['status']=='queued'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.relay_operations').fetchone()['n']==1


def test_gateway_group_callback_routes_to_verification(moderation):
 a=moderation; adapter=VerificationAdapter(); join(a,adapter,restrict=False); notice(a,adapter)
 client,url,headers=gateway(a,a['modbot'])
 assert client.post(url,json=callback_update(adapter),headers=headers).json()['status']=='passed'


def test_gateway_appeal_menu_not_forwarded_to_operator(moderation):
 a=moderation; client,url,headers=gateway(a,a['modbot'])
 from test_appeals import private
 assert client.post(url,json=private(100,'/appeal'),headers=headers).json()['status']=='menu_queued'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.relay_operations').fetchone()['n']==0


def test_gateway_cross_tenant_bot_rejected(service):
 cl,a,c=service
 from conftest import seed
 with psycopg.connect(**DB) as db: foreign=seed(db)
 with pytest.raises(Rejected): dispatch(a['tenant'],foreign['bot'],{'update_id':1})


def test_gateway_malformed_and_unknown_event(service):
 cl,a,c=service; client,url,headers=gateway(a,a['bot'])
 assert client.post(url,json=[],headers=headers).status_code==422
 assert client.post(url,json={'update_id':1,'poll':{}},headers=headers).json()['status']=='ignored'

def test_gateway_and_domain_queue_commit_atomically(service):
 cl,a,c=service; client,url,headers=gateway(a,a['bot'])
 from test_service import message as private
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.quota_accounts SET limit_value=0')
 assert client.post(url,json=private(),headers=headers).status_code==422
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.inbox_events').fetchone()['n']==0
  db.execute('UPDATE tgmate.quota_accounts SET limit_value=1000')
 assert client.post(url,json=private(),headers=headers).json()['status']=='queued'
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.inbox_events').fetchone()['n']==2


def test_gateway_detects_same_bot_update_reused_for_different_content(service):
 cl,a,c=service; client,url,headers=gateway(a,a['bot'])
 from test_service import message as private
 assert client.post(url,json=private(),headers=headers).status_code==200
 changed=private(); changed['message']['text']='modified'
 assert client.post(url,json=changed,headers=headers).status_code==422


def test_nested_transaction_cannot_switch_tenant(service):
 cl,a,c=service
 with transaction(a['tenant']):
  with pytest.raises(ValueError):
   with transaction(uuid.uuid4()): pass

def test_paused_relay_replies_without_forwarding(service):
 cl,a,c=service; client,url,headers=gateway(a,a['bot'])
 from test_service import message as private
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.relay_configs SET status='paused' WHERE id=%s",(a['config'],))
 assert client.post(url,json=private(),headers=headers).json()['status']=='paused_notice_queued'
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.relay_operations').fetchone()['n']==0
  assert db.execute("SELECT count(*) AS n FROM tgmate.outbox_events WHERE event_type='bot.private_reply'").fetchone()['n']==1

def test_same_bot_routes_multiple_groups_through_one_ingress(moderation):
 a=moderation; destination=uuid.uuid4(); group=uuid.uuid4()
 with psycopg.connect(**DB) as db:
  executor=db.execute('SELECT executor_binding_id FROM tgmate.moderation_group_configs WHERE id=%s',(a['group'],)).fetchone()[0]
  db.execute("INSERT INTO tgmate.telegram_destinations(id,tenant_id,telegram_peer_id,destination_type,title) VALUES(%s,%s,-100889,'supergroup','second group')",(destination,a['tenant']))
  db.execute("INSERT INTO tgmate.bot_destination_access(tenant_id,telegram_bot_id,destination_id,membership_status) VALUES(%s,%s,%s,'member')",(a['tenant'],a['modbot'],destination))
  db.execute('INSERT INTO tgmate.moderation_group_configs(id,tenant_id,destination_id,robot_instance_id,telegram_bot_id,executor_binding_id) VALUES(%s,%s,%s,%s,%s,%s)',(group,a['tenant'],destination,a['modrobot'],a['modbot'],executor))
 client,url,headers=gateway(a,a['modbot'])
 assert client.post(url,json=message(),headers=headers).json()['status']=='queued'
 second=message(2); second['message']['chat']['id']=-100889
 assert client.post(url,json=second,headers=headers).json()['status']=='queued'
 with transaction(a['tenant']) as db: assert {row['group_config_id'] for row in db.execute('SELECT group_config_id FROM tgmate.moderation_message_events').fetchall()}=={a['group'],group}

def test_nested_transaction_normalizes_same_tenant_uuid(service):
 cl,a,c=service
 with transaction(str(a['tenant']).upper()) as outer:
  with transaction(a['tenant']) as inner:
   assert inner is outer
   assert inner.execute('SELECT tgmate.current_tenant_id() AS tenant').fetchone()['tenant']==a['tenant']
