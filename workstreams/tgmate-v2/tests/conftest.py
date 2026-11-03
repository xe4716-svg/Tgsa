import json,uuid,os
from pathlib import Path
import psycopg
import pytest
ROOT=Path(__file__).resolve().parents[1]
DB=json.loads(Path(os.environ.get('TGMATE_TEST_OWNER_CONFIG',ROOT/'.local/db.json')).read_text())

def seed(db,kind='bidirectional'):
 t,u,r,b,e,c=[uuid.uuid4() for _ in range(6)]
 db.execute('INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,%s,%s)',(t,'Test',str(t)))
 db.execute("INSERT INTO tgmate.users(id,email) VALUES(%s,%s)",(u,f'{u}@example.test'))
 db.execute("INSERT INTO tgmate.tenant_members(tenant_id,user_id,status) VALUES(%s,%s,'active')",(t,u))
 db.execute('INSERT INTO tgmate.robot_instances(id,tenant_id,product_type_code,name) VALUES(%s,%s,%s,%s)',(r,t,kind,'test'))
 db.execute("INSERT INTO tgmate.telegram_bots(id,tenant_id,token_vault_ref,token_fingerprint,status) VALUES(%s,%s,'test-only',%s,'active')",(b,t,str(b)))
 db.execute("INSERT INTO tgmate.executor_bindings(id,tenant_id,robot_instance_id,telegram_bot_id,purpose) VALUES(%s,%s,%s,%s,'send')",(e,t,r,b))
 db.execute("INSERT INTO tgmate.relay_configs(id,tenant_id,robot_instance_id,telegram_bot_id,executor_binding_id,operator_user_id,status) VALUES(%s,%s,%s,%s,%s,100,'active')",(c,t,r,b,e))
 # Explicit synthetic prior operator authorization; not a Telegram verification.
 import hashlib
 db.execute('INSERT INTO tgmate.relay_operator_authorizations(tenant_id,config_id,operator_user_id,challenge_hash,expires_at,confirmed_at,requested_by) VALUES(%s,%s,100,%s,now(),now(),%s)',(t,c,hashlib.sha256(('fixture:'+str(c)).encode()).hexdigest(),u))
 v=uuid.uuid4()
 db.execute("INSERT INTO tgmate.relay_conversations(id,tenant_id,config_id,customer_user_id,initiated_at,privacy_notice_version) VALUES(%s,%s,%s,200,now(),'v1')",(v,t,c))
 # Synthetic already-delivered notice for the existing conversation only.
 db.execute("INSERT INTO tgmate.relay_privacy_notices(tenant_id,conversation_id,version,confirmed_at,remote_message_id) VALUES(%s,%s,'v1',now(),711)",(t,v))
 db.execute("INSERT INTO tgmate.quota_accounts(tenant_id,entitlement_code,period_start,period_end,limit_value) VALUES(%s,'relay.messages',now()-interval '1 hour',now()+interval '30 days',1000)",(t,))
 return dict(tenant=t,user=u,robot=r,bot=b,binding=e,config=c,conversation=v)

@pytest.fixture
def db():
 with psycopg.connect(**DB) as db:
  yield db
  db.rollback()

@pytest.fixture
def setup(db):
 a=seed(db); b=seed(db)
 db.execute("DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='tgmate_test_runtime') THEN CREATE ROLE tgmate_test_runtime NOSUPERUSER NOBYPASSRLS; END IF; END $$")
 db.execute('GRANT USAGE ON SCHEMA tgmate TO tgmate_test_runtime')
 db.execute('GRANT SELECT,INSERT,UPDATE ON tgmate.relay_configs,tgmate.relay_conversations,tgmate.relay_operations TO tgmate_test_runtime')
 db.execute('GRANT SELECT ON tgmate.robot_instances,tgmate.executor_bindings,tgmate.telegram_bots TO tgmate_test_runtime')
 db.execute('SET LOCAL ROLE tgmate_test_runtime')
 db.execute("SELECT set_config('app.tenant_id',%s,true)",(str(a['tenant']),))
 return a,b

def operation(db,a,**overrides):
 d=dict(id=uuid.uuid4(),tenant_id=a['tenant'],config_id=a['config'],conversation_id=a['conversation'],event_key=str(uuid.uuid4()),direction='to_operator',source_chat_id=200,source_message_id=1,target_chat_id=100,conversation_generation=1,payload_vault_ref='test:payload',generation=1)
 d.update(overrides)
 from psycopg import sql
 q=sql.SQL('INSERT INTO tgmate.relay_operations ({}) VALUES ({}) RETURNING id').format(sql.SQL(',').join(map(sql.Identifier,d)),sql.SQL(',').join(sql.Placeholder() for _ in d))
 return db.execute(q,list(d.values())).fetchone()[0]
