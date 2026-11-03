"""Root Bot worker reuse, restricted role, tenant guard and durable waits on PG."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from cryptography.fernet import Fernet
import pytest

from app.business import db, private_worker
from app.business.telegram_adapters import BotBroadcast
from app.business.worker import RetryLater
from app.telegram.bot_rate_limits import BotRateLimits
from scripts.configure_bot_worker import configure, ROLE

pytestmark=pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION')!='1',reason='disposable database opt-in required')


@pytest.fixture
def database():
    root=Path(__file__).resolve().parents[1]
    owner_url=os.environ['NOTIFICATION_TEST_DATABASE_URL']
    name='tgs_migration_bot_'+uuid4().hex[:12]
    password=secrets.token_urlsafe(32);worker=uuid4()
    params=conninfo_to_dict(owner_url)
    env=dict(os.environ,PGHOST=params['host'],PGPORT=params.get('port','5432'),PGUSER=params['user'],
             PGPASSWORD=params['password'],PGDATABASE=name,APP_DB_PASSWORD=os.environ['APP_DB_PASSWORD'])
    with psycopg.connect(owner_url,dbname='postgres',autocommit=True) as admin:
        assert not admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(ROLE,)).fetchone(), 'Use a dedicated test cluster'
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            result=subprocess.run([sys.executable,str(root/'scripts/bootstrap_combined.py')],env=env,capture_output=True)
            assert result.returncode==0,'Isolated combined initialization failed'
            with psycopg.connect(owner_url,dbname=name,autocommit=True) as owner:
                with owner.transaction():configure(owner,password,worker)
                config={'database':{'conninfo':make_conninfo(owner_url,dbname=name,user=ROLE,password=password)},
                        'vault_key':Fernet.generate_key().decode(),'mode':'live','require_root_tenant_guard':True}
                root_tenant,tenant,other,robot,bot=[uuid4() for _ in range(5)]
                owner.execute("INSERT INTO public.tenants(id,name) VALUES(%s,'root-bot-test')",(root_tenant,))
                owner.execute("INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,'business-bot-test',%s),(%s,'other',%s)",(tenant,str(tenant),other,str(other)))
                owner.execute('INSERT INTO public.integration_tenant_map(tenant_id,business_tenant_id) VALUES(%s,%s)',(root_tenant,tenant))
                owner.execute("INSERT INTO tgmate.robot_instances(id,tenant_id,product_type_code,name) VALUES(%s,%s,'bidirectional','test')",(robot,tenant))
                owner.execute("INSERT INTO tgmate.telegram_bots(id,tenant_id,token_vault_ref,token_fingerprint,status) VALUES(%s,%s,'synthetic','synthetic','active')",(bot,tenant))
                yield owner,config,root_tenant,tenant,other,robot,bot,worker
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(ROLE)))


def test_root_worker_processes_existing_private_outbox_and_rejects_disabled_tenants(database):
    owner,config,root_tenant,tenant,other,robot,bot,worker=database
    task,payload,outbox=[uuid4() for _ in range(3)]
    with db.use_settings(config):
        ciphertext=db.seal({'text':'synthetic private reply'})
    owner.execute("INSERT INTO tgmate.task_registry(id,tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,%s,'bidirectional',%s,'private_reply','queued',%s)",(task,tenant,robot,str(task)))
    owner.execute("INSERT INTO tgmate.relay_payloads(id,tenant_id,ciphertext,expires_at) VALUES(%s,%s,%s,now()+interval '1 day')",(payload,tenant,ciphertext))
    owner.execute("INSERT INTO tgmate.outbox_events(id,tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,%s,'bot',%s,'bot.private_reply',%s,%s)",(outbox,tenant,bot,str(outbox),Jsonb({'task_id':str(task),'payload_id':str(payload),'target_user_id':123})))
    calls=[]
    class Adapter:
        def send_private(self,credential,target,text,markup):
            calls.append((target,text));return 777
    with db.use_settings(config):
        assert private_worker.run_once(tenant,worker,Adapter())['status']=='succeeded'
        assert private_worker.run_once(tenant,worker,Adapter())['status']=='idle'
        with pytest.raises(PermissionError):
            with db.transaction(other):pytest.fail('Unmapped tenant admitted')
        with db.transaction(tenant) as conn:
            assert not conn.execute('SELECT id FROM tgmate.outbox_events WHERE tenant_id=%s',(other,)).fetchall()
            for query in ['SELECT password_hash FROM tgmate.web_credentials','SELECT * FROM public.users',
                          'SELECT * FROM public.telegram_sessions']:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction():conn.execute(query)
        owner.execute("UPDATE public.tenants SET status='SUSPENDED' WHERE id=%s",(root_tenant,))
        with pytest.raises(PermissionError):private_worker.run_once(tenant,worker,Adapter())
    assert calls==[(123,'synthetic private reply')]
    assert owner.execute('SELECT status FROM tgmate.task_registry WHERE id=%s',(task,)).fetchone()[0]=='completed'
    assert owner.execute('SELECT remote_message_ref FROM tgmate.task_deliveries WHERE task_id=%s',(task,)).fetchone()[0]=='777'


def test_bot_server_wait_is_shared_persistent_and_never_shortened(database):
    owner,config,root_tenant,tenant,other,robot,bot,worker=database
    requests=[]
    def response(request):
        requests.append(True)
        return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':200000}})
    with db.use_settings(config), httpx.Client(transport=httpx.MockTransport(response)) as client:
        adapter=BotBroadcast(client,lambda identifier:'synthetic-token',BotRateLimits())
        executor={'tenant_id':tenant,'telegram_bot_id':bot}
        with pytest.raises(RetryLater) as result:adapter.call(executor,'getMe',{})
        assert result.value.seconds==200000
        second=BotBroadcast(client,lambda identifier:'synthetic-token',BotRateLimits())
        with pytest.raises(RetryLater) as result:second.call(executor,'getMe',{})
        assert result.value.seconds>199990 and len(requests)==1
        BotRateLimits().defer(tenant,bot,1)
        remaining=owner.execute('SELECT extract(epoch FROM wait_until-clock_timestamp()) FROM tgmate.bot_runtime_limits WHERE bot_id=%s',(bot,)).fetchone()[0]
        assert remaining>199990
        with db.transaction(tenant) as conn:
            assert not conn.execute('SELECT bot_id FROM tgmate.bot_runtime_limits WHERE tenant_id=%s',(other,)).fetchall()
        # A persistence failure poisons the gate and blocks subsequent HTTP calls.
        owner.execute(sql.SQL('REVOKE SELECT ON tgmate.bot_runtime_limits FROM {}').format(sql.Identifier(ROLE)))
        broken=BotRateLimits()
        with pytest.raises(RuntimeError):broken.check(tenant,bot)
        with pytest.raises(RuntimeError):broken.assert_healthy()
        assert len(requests)==1


def test_encrypted_full_database_restore_preserves_both_ledgers_and_isolation(database,tmp_path,monkeypatch):
    owner,config,root_tenant,tenant,other,robot,bot,worker=database
    from scripts.encrypted_backup import backup_encrypted,restore_encrypted
    from app.security.session_vault import SessionVault,EncryptedSession
    target='tgs_migration_restore_'+uuid4().hex[:12]
    account=uuid4();vault=SessionVault(b'x'*32,'synthetic-recovery-key')
    record=vault.encrypt(root_tenant,account,'synthetic-session-for-recovery')
    owner.execute("INSERT INTO public.tenant_wallet_accounts(tenant_id,currency,balance_minor) VALUES(%s,'USD',12345)",(root_tenant,))
    owner.execute("INSERT INTO public.telegram_accounts(id,tenant_id,status) VALUES(%s,%s,'AUTHORIZED')",(account,root_tenant))
    owner.execute('INSERT INTO public.telegram_sessions(account_id,tenant_id,ciphertext,nonce,wrapped_dek,kms_key_id,encryption_version) VALUES(%s,%s,%s,%s,%s,%s,%s)',(account,root_tenant,record.ciphertext,record.nonce,record.wrapped_dek,record.kms_key_id,record.encryption_version))
    monkeypatch.setenv('PGDATABASE',owner.info.dbname)
    key=tmp_path/'key';key.write_bytes(secrets.token_bytes(32));key.chmod(0o600)
    archive=tmp_path/'full-database.enc'
    backup_encrypted(archive,key)
    try:
        restore_encrypted(archive,target,key)
        with psycopg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'],dbname=target,autocommit=True) as restored:
            for table in ['public.schema_migrations','public.business_schema_migrations']:
                query=sql.SQL('SELECT name,sha256 FROM {} ORDER BY name').format(sql.Identifier(*table.split('.')))
                assert restored.execute(query).fetchall()==owner.execute(query).fetchall()
            assert restored.execute('SELECT tenant_id,business_tenant_id FROM public.integration_tenant_map').fetchall()==[(root_tenant,tenant)]
            assert restored.execute('SELECT id FROM tgmate.telegram_bots').fetchall()==[(bot,)]
            assert restored.execute('SELECT balance_minor FROM public.tenant_wallet_accounts WHERE tenant_id=%s',(root_tenant,)).fetchone()==(12345,)
            saved=restored.execute('SELECT ciphertext,nonce,wrapped_dek,kms_key_id,encryption_version FROM public.telegram_sessions WHERE account_id=%s',(account,)).fetchone()
            assert vault.decrypt(root_tenant,account,EncryptedSession(*saved))=='synthetic-session-for-recovery'
            assert restored.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='tgmate.outbox_events'::regclass").fetchone()==(True,True)
            with restored.transaction():
                restored.execute('SET LOCAL ROLE tgs_bot_worker')
                assert restored.execute('SELECT id FROM tgmate.telegram_bots').fetchall()==[]
                restored.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenant),))
                assert restored.execute('SELECT public.lock_business_worker_tenant(%s)',(tenant,)).fetchone()==(True,)
                assert restored.execute('SELECT id FROM tgmate.telegram_bots').fetchall()==[(bot,)]
        # Authenticated restore still refuses an existing database; source survives.
        with pytest.raises(RuntimeError):restore_encrypted(archive,target,key)
        assert owner.execute('SELECT count(*) FROM tgmate.telegram_bots').fetchone()[0]==1
    finally:
        owner.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(target)))


def test_root_webhook_reuses_durable_authenticated_inbox(database):
    import asyncio
    import hashlib
    from types import SimpleNamespace
    from httpx import AsyncClient,ASGITransport
    from app.bot_main import app
    owner,config,root_tenant,tenant,other,robot,bot,worker=database
    with db.use_settings(config):
        secret=db.seal(['wh1',tenant.hex,bot.hex,'synthetic']).rstrip('=')
        sealed=db.seal({'secret':secret})
    owner.execute("INSERT INTO tgmate.bot_webhook_registrations(tenant_id,bot_id,url,secret_hash,secret_ciphertext,status) VALUES(%s,%s,%s,%s,%s,'active')",(tenant,bot,'https://example.test/webhooks/bots/'+str(bot),hashlib.sha256(secret.encode()).hexdigest(),sealed))
    async def run():
        app.state.business_config={**config,'allowed_tenants':frozenset([str(tenant)])}
        app.state.ingress_slots=asyncio.Semaphore(1);app.state.worker_task=SimpleNamespace(done=lambda:False)
        update={'update_id':17,'message':{'message_id':1,'chat':{'id':123,'type':'private'},'from':{'id':123,'is_bot':False},'text':'synthetic'}}
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            url='/webhooks/bots/'+str(bot);headers={'X-Telegram-Bot-Api-Secret-Token':secret}
            response=await client.post(url,json=update,headers=headers)
            assert response.status_code==200 and response.json()['status']=='ignored_no_private_handler'
            response=await client.post(url,json=update,headers=headers)
            assert response.status_code==200 and response.json()['status']=='duplicate'
            assert (await client.post(url,json={**update,'update_id':18})).status_code==403
            owner.execute("UPDATE public.tenants SET status='SUSPENDED' WHERE id=%s",(root_tenant,))
            assert (await client.post(url,json={**update,'update_id':19},headers=headers)).status_code==403
    asyncio.run(run())
    assert owner.execute('SELECT count(*) FROM tgmate.inbox_events WHERE tenant_id=%s',(tenant,)).fetchone()[0]==1


def test_concurrent_root_workers_claim_each_existing_outbox_item_once(database):
    from concurrent.futures import ThreadPoolExecutor
    import json
    import threading
    import time
    owner,config,root_tenant,tenant,other,robot,bot,worker=database
    count=40;calls=[];lock=threading.Lock()
    with db.use_settings(config):ciphertext=db.seal({'text':'synthetic queue concurrency probe'})
    payload=uuid4()
    owner.execute("INSERT INTO tgmate.relay_payloads(id,tenant_id,ciphertext,expires_at) VALUES(%s,%s,%s,now()+interval '1 day')",(payload,tenant,ciphertext))
    for number in range(count):
        task,event=uuid4(),uuid4()
        owner.execute("INSERT INTO tgmate.task_registry(id,tenant_id,business_module,robot_instance_id,task_type,status,idempotency_key) VALUES(%s,%s,'bidirectional',%s,'private_reply','queued',%s)",(task,tenant,robot,str(task)))
        owner.execute("INSERT INTO tgmate.outbox_events(id,tenant_id,aggregate_type,aggregate_id,event_type,idempotency_key,payload) VALUES(%s,%s,'bot',%s,'bot.private_reply',%s,%s)",(event,tenant,bot,str(event),Jsonb({'task_id':str(task),'payload_id':str(payload),'target_user_id':1000+number})))
    class Adapter:
        def send_private(self,credential,target,text,markup):
            with lock:calls.append(target)
            return target+10000
    def drain(_):
        with db.use_settings(config):
            for _ in range(count+1):
                result=private_worker.run_once(tenant,worker,Adapter())
                if result['status']=='idle':return
            pytest.fail('Queue did not become idle')
    started=time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as executor:list(executor.map(drain,range(8)))
    elapsed=time.perf_counter()-started
    assert len(calls)==count and len(set(calls))==count
    assert owner.execute("SELECT count(*) FROM tgmate.task_registry WHERE tenant_id=%s AND status='completed'",(tenant,)).fetchone()[0]==count
    assert owner.execute("SELECT count(*) FROM tgmate.delivery_attempts WHERE tenant_id=%s AND status='succeeded'",(tenant,)).fetchone()[0]==count
    if os.environ.get('TGMATE_QUEUE_BENCHMARK_REPORT'):
        Path(os.environ['TGMATE_QUEUE_BENCHMARK_REPORT']).write_text(json.dumps({
            'scope':'disposable PostgreSQL queue concurrency; synthetic adapter; no Telegram requests',
            'postgresql':owner.execute('SHOW server_version').fetchone()[0],
            'jobs':count,'threads':8,'unique_sends':len(set(calls)),'seconds':round(elapsed,4),
            'production_capacity_claim':False},indent=2)+'\n')
