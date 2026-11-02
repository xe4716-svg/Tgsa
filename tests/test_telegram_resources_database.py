"""Opt-in real PostgreSQL tests; never use a production database."""
import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.telegram.router import router


@pytest.mark.skipif(os.getenv('TGMATE_RESOURCE_INTEGRATION') != '1', reason='isolated database opt-in required')
def test_real_database_tenant_isolation_and_pagination():
    if os.getenv('TGMATE_RESOURCE_TEST_OWNER_CONFIG'):
        config = json.loads(Path(os.environ['TGMATE_RESOURCE_TEST_OWNER_CONFIG']).read_text())
        config['dbname'] = os.environ['TGMATE_RESOURCE_TEST_DATABASE']
    else:
        config = dict(host=os.getenv('PGHOST', 'localhost'), port=int(os.getenv('PGPORT', '5432')),
                      dbname=os.environ['PGDATABASE'], user=os.environ['PGUSER'],
                      password=os.environ['PGPASSWORD'])
    if config['dbname'] != 'tgmate_ci' and not config['dbname'].startswith('tgs_migration_'):
        pytest.fail('Disposable CI or migration database required')
    config['connect_timeout'] = 5
    role = 'tgs_probe_' + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    tenants = [uuid4(), uuid4()]
    accounts = [uuid4(), uuid4(), uuid4()]
    with psycopg.connect(**config, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        try:
            owner.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
            owner.execute(sql.SQL('GRANT SELECT ON telegram_accounts, proxy_profiles TO {}').format(sql.Identifier(role)))
            for tenant in tenants:
                owner.execute('INSERT INTO tenants(id,name) VALUES (%s,%s)', (tenant, 'resource integration test'))
            for identifier, tenant in zip(accounts, [tenants[0], tenants[0], tenants[1]]):
                owner.execute("INSERT INTO telegram_accounts(id,tenant_id,created_at) VALUES (%s,%s,'2026-01-01T00:00:00Z')", (identifier, tenant))
                owner.execute("INSERT INTO proxy_profiles(tenant_id,proxy_type,host,port) VALUES (%s,'SOCKS5','127.0.0.1',1080)", (tenant,))
            options = dict(host=config.get('host', 'localhost'), port=config.get('port', 5432), database=config['dbname'], user=role, password=password)
            async def check_no_context():
                conn = await asyncpg.connect(**options)
                try:
                    flags = await conn.fetchrow('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')
                    assert not flags['rolsuper'] and not flags['rolbypassrls']
                    assert await conn.fetchval('SELECT COUNT(*) FROM telegram_accounts') == 0
                    assert await conn.fetchval('SELECT COUNT(*) FROM proxy_profiles') == 0
                finally:
                    await conn.close()
            asyncio.run(check_no_context())
            @asynccontextmanager
            async def lifespan(app):
                app.state.db_pool = await asyncpg.create_pool(**options, min_size=1, max_size=1)
                try:
                    yield
                finally:
                    await app.state.db_pool.close()
            app = FastAPI(lifespan=lifespan)
            app.include_router(router)
            principal = AuthPrincipal(uuid4(), tenants[0], uuid4(), 'VIEWER', 'test@example.test', False)
            app.dependency_overrides[get_current_principal] = lambda: principal
            with TestClient(app) as client:
                for path, field, expected in [('/api/v1/telegram/accounts', 'id', set(map(str, accounts[:2]))), ('/api/v1/proxies', 'id', None)]:
                    first = client.get(path, params={'limit': 1, 'tenant_id': str(tenants[1])})
                    assert first.status_code == 200
                    page = first.json()['data']
                    assert len(page['items']) == 1 and page['next_cursor']
                    second = client.get(path, params={'limit': 1, 'cursor': page['next_cursor']})
                    assert second.status_code == 200
                    next_page = second.json()['data']
                    assert len(next_page['items']) == 1 and next_page['next_cursor'] is None
                    ids = {page['items'][0][field], next_page['items'][0][field]}
                    assert len(ids) == 2
                    if expected is not None:
                        assert ids == expected
                principal = AuthPrincipal(uuid4(), tenants[1], uuid4(), 'VIEWER', 'test@example.test', False)
                page = client.get('/api/v1/telegram/accounts').json()['data']
                assert [item['id'] for item in page['items']] == [str(accounts[2])]
        finally:
            for tenant in tenants:
                owner.execute('DELETE FROM tenants WHERE id=%s', (tenant,))
            owner.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            owner.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
