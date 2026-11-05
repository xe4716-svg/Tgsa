"""Disposable PostgreSQL integration; no Telegram clients or network RPCs."""
import asyncio
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

import asyncpg
import psycopg
from psycopg import sql
import pytest

from app.telegram.gateway_command_store import GatewayCommandStore


@pytest.mark.skipif(os.getenv('TGMATE_COMMAND_INTEGRATION') != '1', reason='disposable PostgreSQL opt-in required')
def test_command_roles_mapping_fencing_recovery():
    root = Path(__file__).resolve().parents[1]
    config = dict(host=os.environ['PGHOST'], port=os.environ['PGPORT'], user=os.environ['PGUSER'], dbname='postgres')
    if os.getenv('PGPASSWORD'): config['password'] = os.environ['PGPASSWORD']
    name = 'asd30_' + uuid4().hex[:12]
    submitter, executor = ['asd30_' + uuid4().hex[:12] for _ in range(2)]
    password = secrets.token_urlsafe(32)
    env = dict(os.environ, PGDATABASE=name, APP_DB_PASSWORD=secrets.token_urlsafe(24))
    tenant, business, other, account, key = [uuid4() for _ in range(5)]
    with psycopg.connect(**config, autocommit=True) as admin:
        old = admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname='tgs_runtime'").fetchone()
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            for script in ('migrate.py', 'migrate_business.py'):
                result = subprocess.run([sys.executable, str(root/'scripts'/script), '--psql', os.environ['PG_PSQL']], env=env, capture_output=True)
                assert result.returncode == 0, script + ': ' + result.stderr.decode(errors='replace')
            with psycopg.connect(**dict(config, dbname=name), autocommit=True) as owner:
                for role, group in ((submitter, 'tgs_command_submitter'), (executor, 'tgs_command_executor')):
                    owner.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
                    owner.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(group), sql.Identifier(role)))
                owner.execute("INSERT INTO tenants(id,name) VALUES(%s,'test'),(%s,'other')", (tenant, other))
                owner.execute("INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,'test',%s)", (business,str(business)))
                owner.execute('INSERT INTO integration_tenant_map VALUES(%s,%s)', (tenant,business))
                owner.execute("INSERT INTO telegram_accounts(id,tenant_id,status,connection_mode) VALUES(%s,%s,'ACTIVE','DIRECT')", (account,tenant))
                owner.execute('INSERT INTO account_gateway_routes(tenant_id,account_id,enabled) VALUES(%s,%s,true)', (tenant,account))
                owner.execute("INSERT INTO account_gateway_leases(tenant_id,account_id,worker_id,fencing_token,lease_expires_at) VALUES(%s,%s,'gateway-test',7,now()+interval '5 minutes')", (tenant,account))
                owner.execute("INSERT INTO gateway_command_principals VALUES(%s,%s,'submit'),(%s,%s,'execute')", (submitter,tenant,executor,tenant))

                async def exercise():
                    options=dict(host=config['host'],port=int(config['port']),database=name,min_size=1,max_size=2,password=password)
                    async with asyncpg.create_pool(**options,user=submitter) as sp, asyncpg.create_pool(**options,user=executor) as ep:
                        s,e=GatewayCommandStore(sp),GatewayCommandStore(ep)
                        async with sp.acquire() as c:
                            for query in ('SELECT * FROM gateway_account_commands','SELECT * FROM telegram_sessions', "UPDATE gateway_command_principals SET capability='execute'",'SET ROLE tgs_command_owner'):
                                with pytest.raises(asyncpg.InsufficientPrivilegeError): await c.execute(query)
                        with pytest.raises(asyncpg.InsufficientPrivilegeError):
                            await s.submit_text(other,account,1,key,123,'hello')
                        with pytest.raises(asyncpg.InsufficientPrivilegeError):
                            await s.claim_next(tenant,account,'gateway-test',7)
                        with pytest.raises(asyncpg.InsufficientPrivilegeError):
                            await e.submit_text(business,account,1,key,123,'hello')
                        # Root IDs must not be accepted as business IDs.
                        with pytest.raises(asyncpg.InsufficientPrivilegeError):
                            await s.submit_text(tenant,account,1,key,123,'hello')
                        command_id=await s.submit_text(business,account,1,key,123,'hello')
                        assert await s.submit_text(business,account,1,key,123,'hello') == command_id
                        with pytest.raises(asyncpg.UniqueViolationError):
                            await s.submit_text(business,account,1,key,123,'changed')
                        assert await e.claim_next(tenant,account,'gateway-test',6) is None
                        claims=await asyncio.gather(*(e.claim_next(tenant,account,'gateway-test',7) for _ in range(2)))
                        command=next(c for c in claims if c is not None)
                        assert sum(c is not None for c in claims)==1
                        assert command.payload == {'text':'hello'}
                        args=(tenant,command.id,'gateway-test',7)
                        assert not await e.finish(*args,'succeeded',remote_message_id=10)
                        owner.execute("UPDATE telegram_accounts SET status='PAUSED' WHERE id=%s", (account,))
                        assert not await e.mark_dispatch_started(*args)
                        owner.execute("UPDATE telegram_accounts SET status='ACTIVE' WHERE id=%s", (account,))
                        owner.execute('UPDATE account_gateway_routes SET generation=2 WHERE account_id=%s',(account,))
                        assert not await e.mark_dispatch_started(*args)
                        owner.execute('UPDATE account_gateway_routes SET generation=1 WHERE account_id=%s',(account,))
                        assert await e.mark_dispatch_started(*args)
                        assert not await e.mark_dispatch_started(*args)
                        assert not await e.finish(tenant,command.id,'gateway-test',6,'succeeded',remote_message_id=10)
                        assert await e.finish(*args,'succeeded',remote_message_id=10)
                        assert not await e.finish(*args,'succeeded',remote_message_id=10)
                        # Exercise real SQL transitions through the executor with a fake RPC.
                        from app.telegram.gateway_command_executor import GatewayCommandExecutor
                        from app.telegram.gateway_leases import AccountLease
                        from unittest.mock import AsyncMock
                        synthetic=await s.submit_text(business,account,1,uuid4(),123,'fake rpc')
                        sender=AsyncMock(return_value=22)
                        async def controlled(operation,timeout): return await asyncio.wait_for(operation(),timeout)
                        assert await GatewayCommandExecutor(e,sender).execute_one(AccountLease(tenant,account,'gateway-test',7),1,object(),controlled,2)
                        assert owner.execute('SELECT status FROM gateway_account_commands WHERE id=%s',(synthetic,)).fetchone()[0]=='succeeded'
                        sender.assert_awaited_once()
                        # FloodWait is a known non-delivery result: hold the command
                        # until retry_after, then clear the old dispatch marker and
                        # allow exactly one new fenced claim.
                        flood_id=await s.submit_text(business,account,1,uuid4(),123,'flood wait')
                        flood_claim=await e.claim_next(tenant,account,'gateway-test',7)
                        assert flood_claim.id==flood_id
                        flood_args=(tenant,flood_id,'gateway-test',7)
                        assert await e.mark_dispatch_started(*flood_args)
                        assert await e.finish(*flood_args,'flood_wait',retry_after=120)
                        state=owner.execute(
                            'SELECT status,available_at>now(),completed_at,dispatch_started_at '
                            'FROM gateway_account_commands WHERE id=%s',(flood_id,)
                        ).fetchone()
                        assert state[0]=='flood_wait' and state[1] and state[2] is None and state[3] is not None
                        assert await e.requeue_flood_wait(tenant,account,'gateway-test',7)==0
                        owner.execute(
                            "UPDATE gateway_account_commands SET available_at=now()-interval '1 second' WHERE id=%s",
                            (flood_id,),
                        )
                        assert await e.requeue_flood_wait(tenant,account,'gateway-test',7)==1
                        state=owner.execute(
                            'SELECT status,completed_at,dispatch_started_at FROM gateway_account_commands WHERE id=%s',
                            (flood_id,)
                        ).fetchone()
                        assert state==('queued',None,None)
                        retry_claim=await e.claim_next(tenant,account,'gateway-test',7)
                        assert retry_claim.id==flood_id
                        assert await e.mark_dispatch_started(*flood_args)
                        assert await e.finish(*flood_args,'succeeded',remote_message_id=33)
                        transitions=owner.execute(
                            "SELECT transition FROM gateway_account_command_events WHERE command_id=%s ORDER BY id",
                            (flood_id,)
                        ).fetchall()
                        assert [row[0] for row in transitions]==[
                            'submitted','claimed','dispatch_started','flood_wait',
                            'requeued','claimed','dispatch_started','succeeded'
                        ]
                        # Crash before dispatch requeues; crash after mark never retries.
                        second=await s.submit_text(business,account,1,uuid4(),123,'second')
                        assert (await e.claim_next(tenant,account,'gateway-test',7)).id==second
                        owner.execute("UPDATE gateway_account_commands SET claim_until=now()-interval '1 second' WHERE id=%s",(second,))
                        assert await e.recover_expired(tenant,account,'gateway-test',7)==(1,0)
                        assert (await e.claim_next(tenant,account,'gateway-test',7)).id==second
                        assert await e.mark_dispatch_started(tenant,second,'gateway-test',7)
                        owner.execute("UPDATE gateway_account_commands SET claim_until=now()-interval '1 second' WHERE id=%s",(second,))
                        assert await e.recover_expired(tenant,account,'gateway-test',7)==(0,1)
                        await s.submit_text(business,account,1,uuid4(),123,'blocked by unknown')
                        assert await e.claim_next(tenant,account,'gateway-test',7) is None
                        owner.execute("UPDATE tgmate.tenants SET status='suspended' WHERE id=%s",(business,))
                        with pytest.raises(asyncpg.InsufficientPrivilegeError):
                            await s.submit_text(business,account,1,uuid4(),123,'denied')
                        with pytest.raises(psycopg.errors.RaiseException):
                            owner.execute("UPDATE gateway_account_command_events SET metadata='{}'")
                asyncio.run(exercise())
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in (submitter,executor):
                admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
            if old:
                admin.execute(sql.SQL('ALTER ROLE tgs_runtime PASSWORD {}').format(sql.Literal(old[0])))
