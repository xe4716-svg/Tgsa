"""Persist authorized sessions against real RLS-protected PostgreSQL."""
import asyncio
import os
import secrets
from uuid import uuid4

import asyncpg
import pytest

from app.security.session_vault import SessionVault, EncryptedSession
from app.telegram.account_persistence import persist_authorized_account
from app.telegram.proxy_revision import proxy_revision


@pytest.mark.skipif(os.getenv('TGMATE_LOGIN_INTEGRATION') != '1', reason='disposable database opt-in required')
def test_account_session_proxy_and_consent_commit_atomically():
    async def run():
        owner = await asyncpg.connect(os.environ['NOTIFICATION_TEST_DATABASE_URL'])
        runtime = await asyncpg.create_pool(os.environ['NOTIFICATION_RUNTIME_DATABASE_URL'], min_size=1, max_size=1)
        tenant, user, proxy = uuid4(), uuid4(), uuid4()
        other_tenant = uuid4()
        telegram_id = secrets.randbelow(2**50)+1
        vault = SessionVault(b'x'*32, 'test-key')
        try:
            await owner.execute("INSERT INTO tenants(id,name) VALUES($1,'login-test'),($2,'other-test')",tenant,other_tenant)
            await owner.execute("INSERT INTO users(id,auth_subject) VALUES($1,$1::uuid::text)",user)
            await owner.execute("INSERT INTO tenant_memberships(tenant_id,user_id,role) VALUES($1,$2,'OWNER')",tenant,user)
            await owner.execute("INSERT INTO proxy_profiles(id,tenant_id,proxy_type,host,port,verification_status,verified_sharing_mode) VALUES($1,$2,'SOCKS5','proxy.example.test',1080,'VERIFIED','EXCLUSIVE')",proxy,tenant)
            profile=dict(proxy_type='SOCKS5',host='proxy.example.test',port=1080,verification_status='VERIFIED',status='ACTIVE')
            snapshot=await owner.fetchrow('SELECT * FROM proxy_profiles WHERE id=$1',proxy)
            profile['_revision']=proxy_revision(dict(snapshot))
            kwargs=dict(pool=runtime,tenant_id=tenant,user_id=user,account={'telegram_user_id':telegram_id},session_string='synthetic-session-secret',vault=vault,connection_mode='PROXY',proxy_profile=profile,proxy_profile_id=proxy,notice_version='v1.0')
            with pytest.raises(ValueError,match='verified and active'):
                await persist_authorized_account(**{**kwargs,'proxy_profile_id':uuid4()})
            assert await owner.fetchval('SELECT count(*) FROM telegram_accounts WHERE tenant_id=$1',tenant)==0
            await owner.execute("UPDATE proxy_profiles SET host='changed.example.test' WHERE id=$1",proxy)
            with pytest.raises(ValueError,match='configuration changed'):
                await persist_authorized_account(**kwargs)
            assert await owner.fetchval('SELECT count(*) FROM telegram_accounts WHERE tenant_id=$1',tenant)==0
            await owner.execute("UPDATE proxy_profiles SET host='proxy.example.test' WHERE id=$1",proxy)
            account_id = await persist_authorized_account(**kwargs)
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
