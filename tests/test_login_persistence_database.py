"""Persist authorized sessions against real RLS-protected PostgreSQL."""
import asyncio
import os
import secrets
from uuid import uuid4

import asyncpg
import pytest

from app.security.session_vault import SessionVault, EncryptedSession
from app.telegram.account_persistence import persist_authorized_account, finish_authorized_account
from app.telegram.proxy_revision import proxy_revision


@pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION') != '1', reason='disposable database opt-in required')
def test_account_session_proxy_and_consent_commit_atomically():
    async def run():
        owner = await asyncpg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'])
        runtime = await asyncpg.create_pool(os.environ['NOTIFICATION_RUNTIME_DATABASE_URL'], min_size=1, max_size=1)
        tenant, user, proxy = uuid4(), uuid4(), uuid4()
        family_id = uuid4()
        other_tenant = uuid4()
        telegram_id = secrets.randbelow(2**50)+1
        vault = SessionVault(b'x'*32, 'test-key')
        try:
            await owner.execute("INSERT INTO tenants(id,name) VALUES($1,'login-test'),($2,'other-test')",tenant,other_tenant)
            await owner.execute("INSERT INTO users(id,auth_subject) VALUES($1,$1::uuid::text)",user)
            await owner.execute("INSERT INTO tenant_memberships(tenant_id,user_id,role) VALUES($1,$2,'OWNER')",tenant,user)
            await owner.execute("INSERT INTO proxy_profiles(id,tenant_id,proxy_type,host,port,verification_status,verified_sharing_mode) VALUES($1,$2,'SOCKS5','proxy.example.test',1080,'VERIFIED','EXCLUSIVE')",proxy,tenant)
            await owner.execute("INSERT INTO auth_refresh_tokens(token_hash,tenant_id,user_id,family_id,expires_at) VALUES($1,$2,$3,$4,clock_timestamp()+interval '1 day')",secrets.token_hex(32),tenant,user,family_id)
            profile=dict(proxy_type='SOCKS5',host='proxy.example.test',port=1080,verification_status='VERIFIED',status='ACTIVE')
            snapshot=await owner.fetchrow('SELECT * FROM proxy_profiles WHERE id=$1',proxy)
            profile['_revision']=proxy_revision(dict(snapshot))
            kwargs=dict(pool=runtime,tenant_id=tenant,user_id=user,family_id=family_id,account={'telegram_user_id':telegram_id},session_string='synthetic-session-secret',vault=vault,connection_mode='PROXY',proxy_profile=profile,proxy_profile_id=proxy,notice_version='v1.0')
            with pytest.raises(ValueError,match='verified and active'):
                await persist_authorized_account(**{**kwargs,'proxy_profile_id':uuid4()})
            assert await owner.fetchval('SELECT count(*) FROM telegram_accounts WHERE tenant_id=$1',tenant)==0
            await owner.execute("UPDATE proxy_profiles SET host='changed.example.test' WHERE id=$1",proxy)
            with pytest.raises(ValueError,match='configuration changed'):
                await persist_authorized_account(**kwargs)
            assert await owner.fetchval('SELECT count(*) FROM telegram_accounts WHERE tenant_id=$1',tenant)==0
            await owner.execute("UPDATE proxy_profiles SET host='proxy.example.test' WHERE id=$1",proxy)
            account_id = await persist_authorized_account(**kwargs)
            from app.telegram.gateway_leases import GatewayLeases
            assert await owner.fetchval('SELECT status FROM telegram_accounts WHERE id=$1',account_id)=='PENDING'
            assert await GatewayLeases(runtime).acquire(tenant,account_id,'premature') is None
            assert await finish_authorized_account(runtime,tenant_id=tenant,user_id=user,family_id=family_id,account_id=account_id)
            assert await finish_authorized_account(runtime,tenant_id=tenant,user_id=user,family_id=family_id,account_id=account_id)
            assert await owner.fetchval('SELECT status FROM telegram_accounts WHERE id=$1',account_id)=='AUTHORIZED'
            row=await owner.fetchrow('SELECT * FROM telegram_sessions WHERE account_id=$1',account_id)
            assert b'synthetic-session-secret' not in bytes(row['ciphertext'])
            record=EncryptedSession(**{k:row[k] for k in ('ciphertext','nonce','wrapped_dek','kms_key_id','encryption_version')})
            assert vault.decrypt(tenant,account_id,record)=='synthetic-session-secret'
            binding=await owner.fetchrow('SELECT proxy_profile_id,allocation_mode FROM account_proxy_bindings WHERE account_id=$1',account_id)
            assert binding['proxy_profile_id']==proxy and binding['allocation_mode']=='EXCLUSIVE'
            assert await owner.fetchval('SELECT security_state FROM telegram_accounts WHERE id=$1',account_id)=='NEW_DEVICE'
            assert await owner.fetchval('SELECT count(*) FROM connection_consents WHERE tenant_id=$1 AND user_id=$2',tenant,user)==1
            assert await owner.fetchval("SELECT count(*) FROM audit_logs WHERE tenant_id=$1 AND action='TELEGRAM_ACCOUNT_CONNECTED'",tenant)==1
            # One-account free-plan quota is rechecked under the tenant lock.
            with pytest.raises(ValueError,match='limit reached'):
                await persist_authorized_account(**{**kwargs,'account':{'telegram_user_id':telegram_id+1}})
            assert await owner.fetchval('SELECT count(*) FROM telegram_accounts WHERE tenant_id=$1',tenant)==1
            with pytest.raises(PermissionError):
                await persist_authorized_account(**{**kwargs,'tenant_id':other_tenant})
            async with runtime.acquire() as conn:
                assert await conn.fetchval('SELECT count(*) FROM telegram_sessions')==0
                assert await conn.fetchval('SELECT count(*) FROM account_proxy_bindings')==0
        finally:
            await runtime.close()
            await owner.execute('DELETE FROM tenants WHERE id=ANY($1::uuid[])',[tenant,other_tenant])
            await owner.execute('DELETE FROM users WHERE id=$1',user)
            await owner.close()
    asyncio.run(run())


@pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION') != '1', reason='disposable database opt-in required')
@pytest.mark.parametrize('change',['viewer','remove','disabled','revoked','expired'])
def test_proxy_lock_wait_rechecks_current_write_authority(change):
    async def run():
        owner=await asyncpg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'])
        app_name='login-race-'+uuid4().hex
        pool=await asyncpg.create_pool(os.environ['NOTIFICATION_RUNTIME_DATABASE_URL'],min_size=1,max_size=1,server_settings={'application_name':app_name})
        tenant,user,proxy,family=[uuid4() for _ in range(4)]
        token=secrets.token_hex(32)
        task=None;tx=None
        try:
            await owner.execute("INSERT INTO tenants(id,name) VALUES($1,'race-test')",tenant)
            await owner.execute('INSERT INTO users(id,auth_subject) VALUES($1,$1::uuid::text)',user)
            await owner.execute("INSERT INTO tenant_memberships(tenant_id,user_id,role) VALUES($1,$2,'OWNER')",tenant,user)
            await owner.execute("INSERT INTO auth_refresh_tokens(token_hash,tenant_id,user_id,family_id,expires_at) VALUES($1,$2,$3,$4,clock_timestamp()+interval '1 day')",token,tenant,user,family)
            await owner.execute("INSERT INTO proxy_profiles(id,tenant_id,proxy_type,host,port,verification_status) VALUES($1,$2,'SOCKS5','proxy.example.test',1080,'VERIFIED')",proxy,tenant)
            row=await owner.fetchrow('SELECT * FROM proxy_profiles WHERE id=$1',proxy)
            profile=dict(row);profile['_revision']=proxy_revision(profile)
            tx=owner.transaction();await tx.start()
            await owner.execute('SELECT id FROM proxy_profiles WHERE id=$1 FOR UPDATE',proxy)
            task=asyncio.create_task(persist_authorized_account(pool,tenant_id=tenant,user_id=user,family_id=family,
                account={'telegram_user_id':secrets.randbelow(2**50)+1},session_string='synthetic',vault=SessionVault(b'x'*32,'test-key'),
                connection_mode='PROXY',proxy_profile=profile,proxy_profile_id=proxy,notice_version='v1.0'))
            for _ in range(200):
                await owner.execute('SELECT pg_stat_clear_snapshot()')
                if await owner.fetchval("SELECT count(*) FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock'",app_name):
                    break
                await asyncio.sleep(0.01)
            else:pytest.fail('Persistence did not block on the controlled proxy lock')
            if change=='viewer':await owner.execute("UPDATE tenant_memberships SET role='VIEWER' WHERE tenant_id=$1 AND user_id=$2",tenant,user)
            elif change=='remove':await owner.execute('DELETE FROM tenant_memberships WHERE tenant_id=$1 AND user_id=$2',tenant,user)
            elif change=='disabled':await owner.execute("UPDATE users SET status='DISABLED' WHERE id=$1",user)
            elif change=='revoked':await owner.execute('UPDATE auth_refresh_tokens SET revoked_at=clock_timestamp() WHERE token_hash=$1',token)
            else:await owner.execute("UPDATE auth_refresh_tokens SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=$1",token)
            await tx.commit();tx=None
            with pytest.raises(PermissionError):await asyncio.wait_for(task,5)
            for table in ['telegram_accounts','telegram_sessions','account_proxy_bindings','connection_consents','audit_logs']:
                assert await owner.fetchval(f'SELECT count(*) FROM {table} WHERE tenant_id=$1',tenant)==0
        finally:
            if tx:await tx.rollback()
            if task and not task.done():
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            await pool.close()
            await owner.execute('DELETE FROM tenants WHERE id=$1',tenant)
            await owner.execute('DELETE FROM users WHERE id=$1',user)
            await owner.close()
    asyncio.run(run())


@pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION') != '1', reason='disposable database opt-in required')
def test_gateway_lease_ownership_fencing_and_rls():
    from dataclasses import replace
    from app.telegram.gateway_leases import GatewayLeases,LeaseLost
    async def run():
        owner=await asyncpg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'])
        pool=await asyncpg.create_pool(os.environ['NOTIFICATION_RUNTIME_DATABASE_URL'],min_size=1,max_size=12)
        tenant,other,account=[uuid4() for _ in range(3)]
        leases=GatewayLeases(pool)
        try:
            await owner.execute("INSERT INTO tenants(id,name) VALUES($1,'lease-test'),($2,'other-lease')",tenant,other)
            await owner.execute("INSERT INTO telegram_accounts(id,tenant_id,status) VALUES($1,$2,'AUTHORIZED')",account,tenant)
            claims=await asyncio.gather(*(leases.acquire(tenant,account,'worker-'+str(i)) for i in range(12)))
            live=[claim for claim in claims if claim is not None]
            assert len(live)==1
            first=live[0]
            assert await leases.renew(first)
            assert await leases.acquire(other,account,'wrong-tenant') is None
            assert not await leases.renew(replace(first,tenant_id=other))
            async with leases.fenced_transaction(first) as conn:
                await conn.execute("INSERT INTO account_security_events(tenant_id,account_id,event_type,severity) VALUES($1,$2,'LEASE_TEST','INFO')",tenant,account)
            assert await leases.release(first)
            second=await leases.acquire(tenant,account,'next-worker')
            assert second.fencing_token==first.fencing_token+1
            assert not await leases.renew(first) and not await leases.release(first)
            with pytest.raises(LeaseLost):
                async with leases.fenced_transaction(first):pytest.fail('Stale worker entered transaction')
            # Expiry during a fenced transaction must roll back its writes.
            with pytest.raises(LeaseLost):
                async with leases.fenced_transaction(second) as conn:
                    await conn.execute("INSERT INTO account_security_events(tenant_id,account_id,event_type,severity) VALUES($1,$2,'ROLLBACK_PROBE','INFO')",tenant,account)
                    await conn.execute("UPDATE account_gateway_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE account_id=$1",account)
            assert await owner.fetchval("SELECT count(*) FROM account_security_events WHERE tenant_id=$1 AND event_type='ROLLBACK_PROBE'",tenant)==0
            await owner.execute("UPDATE account_gateway_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE account_id=$1",account)
            assert not await leases.renew(second)
            third=await leases.acquire(tenant,account,'third-worker')
            assert third.fencing_token==second.fencing_token+1
            await owner.execute("UPDATE telegram_accounts SET status='FROZEN' WHERE id=$1",account)
            assert not await leases.renew(third)
            with pytest.raises(LeaseLost):
                async with leases.fenced_transaction(third):pytest.fail('Frozen account accepted')
            assert await leases.release(third)
            async with pool.acquire() as conn:
                assert await conn.fetchval('SELECT count(*) FROM account_gateway_leases')==0
        finally:
            await pool.close()
            await owner.execute('DELETE FROM tenants WHERE id=ANY($1::uuid[])',[tenant,other])
            await owner.close()
    asyncio.run(run())


@pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION') != '1', reason='disposable database opt-in required')
@pytest.mark.parametrize('mode,fault',[('DIRECT','material'),('PROXY','material'),('DIRECT','disconnect'),('PROXY','disconnect'),('DIRECT','paused'),('PROXY','paused'),('PROXY','proxy_invalid')])
def test_owned_gateway_loads_encrypted_material_and_stops_when_it_changes(mode,fault):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.security.proxy_vault import ProxyVault
    from app.telegram.gateway_runtime import GatewayRunner,GatewayCloseUncertain
    async def run():
        owner=await asyncpg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'])
        pool=await asyncpg.create_pool(os.environ['NOTIFICATION_RUNTIME_DATABASE_URL'],min_size=1,max_size=5)
        tenant,account,proxy=[uuid4() for _ in range(3)]
        telegram_id=secrets.randbelow(2**50)+1
        vault=SessionVault(b'x'*32,'test-key');session='private-synthetic-session'
        client=SimpleNamespace(connect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=SimpleNamespace(id=telegram_id)),disconnect=AsyncMock(),
            disconnected=asyncio.get_running_loop().create_future())
        task=None;stop=asyncio.Event();ready=asyncio.Event()
        try:
            await owner.execute("INSERT INTO tenants(id,name) VALUES($1,'owned-gateway')",tenant)
            await owner.execute("INSERT INTO telegram_accounts(id,tenant_id,telegram_user_id,status,connection_mode) VALUES($1,$2,$3,'AUTHORIZED',$4)",account,tenant,telegram_id,mode)
            record=vault.encrypt(tenant,account,session)
            await owner.execute('INSERT INTO telegram_sessions(account_id,tenant_id,ciphertext,nonce,wrapped_dek,kms_key_id,encryption_version) VALUES($1,$2,$3,$4,$5,$6,$7)',account,tenant,record.ciphertext,record.nonce,record.wrapped_dek,record.kms_key_id,record.encryption_version)
            if mode=='PROXY':
                blob=ProxyVault(b'x'*32,'test-key').encode_credentials(tenant,proxy,{'username':'synthetic','password':'private-proxy-password'})
                await owner.execute("INSERT INTO proxy_profiles(id,tenant_id,proxy_type,host,port,credentials_ciphertext,secret_version,verification_status) VALUES($1,$2,'SOCKS5','proxy.example.test',1080,$3,'test-key','VERIFIED')",proxy,tenant,blob)
                await owner.execute("INSERT INTO account_proxy_bindings(tenant_id,account_id,proxy_profile_id,allocation_mode) VALUES($1,$2,$3,'SHARED')",tenant,account,proxy)
            def factory(value,**kwargs):
                assert value==session and kwargs['managed'] is True and kwargs['connection_mode']==mode
                if mode=='PROXY':assert kwargs['proxy_profile']['password']=='private-proxy-password'
                else:assert kwargs['proxy_profile'] is None
                return client
            runner=GatewayRunner(pool,vault,'owned-test',ttl=10,heartbeat=.02,control_timeout=1,rpc_timeout=2,client_factory=factory)
            task=asyncio.create_task(runner.run(tenant,account,stop,ready=ready))
            await asyncio.wait_for(ready.wait(),5)
            assert await owner.fetchval('SELECT security_state FROM telegram_accounts WHERE id=$1',account)=='NEW_DEVICE'
            if fault=='disconnect':
                client.disconnect.side_effect=OSError('synthetic close failure')
                stop.set()
                with pytest.raises(GatewayCloseUncertain):await asyncio.wait_for(task,5)
                state=await owner.fetchrow('SELECT status,security_state FROM telegram_accounts WHERE id=$1',account)
                assert dict(state)=={'status':'PAUSED','security_state':'GATEWAY_CLOSE_UNCERTAIN'}
                assert await owner.fetchval("SELECT count(*) FROM account_security_events WHERE tenant_id=$1 AND severity='CRITICAL'",tenant)==1
                assert await owner.fetchval("SELECT count(*) FROM audit_logs WHERE tenant_id=$1 AND action='GATEWAY_DISCONNECT_UNCERTAIN'",tenant)==1
                assert await owner.fetchval('SELECT lease_expires_at>clock_timestamp() FROM account_gateway_leases WHERE account_id=$1',account)
                return
            if fault=='paused':await owner.execute("UPDATE telegram_accounts SET status='PAUSED' WHERE id=$1",account)
            elif fault=='proxy_invalid':await owner.execute("UPDATE proxy_profiles SET status='FAILED' WHERE id=$1",proxy)
            elif mode=='PROXY':await owner.execute("UPDATE proxy_profiles SET host='changed.example.test' WHERE id=$1",proxy)
            else:await owner.execute("UPDATE telegram_sessions SET nonce=$1 WHERE account_id=$2",b'z'*12,account)
            result=await asyncio.wait_for(task,5)
            assert result.reason=={'material':'MATERIAL_CHANGED','paused':'OWNERSHIP_LOST','proxy_invalid':'MATERIAL_UNAVAILABLE'}[fault]
            client.disconnect.assert_awaited_once();client.get_me.assert_awaited_once()
            assert await owner.fetchval('SELECT lease_expires_at<=clock_timestamp() FROM account_gateway_leases WHERE account_id=$1',account)
            # The runner cannot turn read-only NEW_DEVICE into publishing rights.
            assert await owner.fetchval('SELECT security_state FROM telegram_accounts WHERE id=$1',account)=='NEW_DEVICE'
        finally:
            stop.set()
            if task and not task.done():await asyncio.wait_for(task,5)
            await pool.close()
            await owner.execute('DELETE FROM tenants WHERE id=$1',tenant)
            await owner.close()
    asyncio.run(run())


@pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION') != '1', reason='disposable database opt-in required')
def test_gateway_server_wait_is_durable_and_fences_previous_owner():
    from app.telegram.gateway_leases import GatewayLeases
    async def run():
        owner=await asyncpg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'])
        pool=await asyncpg.create_pool(os.environ['NOTIFICATION_RUNTIME_DATABASE_URL'],min_size=1,max_size=2)
        tenant,account=uuid4(),uuid4()
        store=GatewayLeases(pool)
        try:
            await owner.execute("INSERT INTO tenants(id,name) VALUES($1,'gateway-wait')",tenant)
            await owner.execute("INSERT INTO telegram_accounts(id,tenant_id,status) VALUES($1,$2,'AUTHORIZED')",account,tenant)
            first=await store.acquire(tenant,account,'worker-a')
            assert await store.defer(first,120)
            assert not await store.renew(first) and not await store.release(first)
            second_process=GatewayLeases(pool)
            assert await second_process.acquire(tenant,account,'worker-b') is None
            wait=await owner.fetchval('SELECT extract(epoch FROM lease_expires_at-clock_timestamp()) FROM account_gateway_leases WHERE account_id=$1',account)
            assert wait>115
            await owner.execute("UPDATE account_gateway_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE account_id=$1",account)
            next_lease=await second_process.acquire(tenant,account,'worker-b')
            assert next_lease.fencing_token==first.fencing_token+2
            assert not await store.defer(first,1000)
        finally:
            await pool.close();await owner.execute('DELETE FROM tenants WHERE id=$1',tenant);await owner.close()
    asyncio.run(run())
