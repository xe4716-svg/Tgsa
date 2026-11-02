from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
import pytest
import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.telegram.router import router, ACCOUNT_FIELDS, PROXY_FIELDS


@pytest.fixture
def setup():
    principal = AuthPrincipal(uuid4(), uuid4(), uuid4(), 'VIEWER', 'test@example.test', False)
    conn = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(return_value=[]))
    @asynccontextmanager
    async def context():
        yield conn
    conn.transaction = context
    app = FastAPI()
    app.include_router(router)
    app.state.db_pool = SimpleNamespace(acquire=context)
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app, conn, principal


@pytest.mark.parametrize('path,table,fields', [
    ('/api/v1/telegram/accounts', 'telegram_accounts', ACCOUNT_FIELDS),
    ('/api/v1/proxies', 'proxy_profiles', PROXY_FIELDS),
])
def test_inventory_tenant_scope_and_secret_exclusion(setup, path, table, fields):
    app, conn, principal = setup
    row = dict.fromkeys(fields, None)
    row.update(id=uuid4(), created_at=datetime.now(timezone.utc), token='secret', ciphertext='secret', credentials_ciphertext='secret')
    conn.fetch.return_value = [row, row]
    with TestClient(app) as client:
        response = client.get(path, params={'limit': 1, 'tenant_id': str(uuid4())})
        assert response.status_code == 200
        envelope = response.json()
        assert envelope['success'] is True
        UUID(envelope['request_id'])
        body = envelope['data']
        assert set(body['items'][0]) == set(fields)
        assert 'secret' not in response.text
        cursor = body['next_cursor']
        assert cursor
        response = client.get(path, params={'cursor': cursor})
        assert response.status_code == 200
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
    sql, tenant, timestamp, identifier, limit = conn.fetch.call_args.args
    assert f'FROM {table}' in sql
    assert 'WHERE tenant_id = $1' in sql
    assert '*' not in sql
    assert tenant == principal.tenant_id
    assert timestamp == row['created_at'] and identifier == row['id']
    assert limit == 21


@pytest.mark.parametrize('path', ['/api/v1/telegram/accounts', '/api/v1/proxies'])
@pytest.mark.parametrize('params,status', [({'limit': 0}, 422), ({'limit': 101}, 422),
    ({'limit': 'bad'}, 422), ({'cursor': 'bad'}, 400), ({'cursor': 'bnVsbA=='}, 400)])
def test_invalid_pagination_never_queries(setup, path, params, status):
    app, conn, _ = setup
    with TestClient(app) as client:
        response = client.get(path, params=params)
        assert response.status_code == status
        envelope = response.json()
        assert envelope['success'] is False
        UUID(envelope['request_id'])
        assert envelope['error']['code'] == ('VALIDATION_ERROR' if status == 422 else 'INVALID_REQUEST')
        assert envelope['error']['retryable'] is False
        assert envelope['error']['message']
    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize('path', ['/api/v1/telegram/accounts', '/api/v1/proxies'])
def test_requires_authentication(setup, path):
    app, conn, _ = setup
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers['www-authenticate'] == 'Bearer'
        envelope = response.json()
        assert envelope['success'] is False
        UUID(envelope['request_id'])
        assert envelope['error']['code'] == 'UNAUTHENTICATED'
        assert envelope['error']['retryable'] is False
    conn.fetch.assert_not_awaited()


def test_request_id_is_generated_and_legacy_routes_unchanged(setup):
    from fastapi import HTTPException
    app, _, _ = setup
    @app.get('/legacy')
    async def legacy():
        raise HTTPException(status_code=400, detail='legacy detail')
    with TestClient(app) as client:
        first = client.get('/api/v1/proxies', headers={'X-Request-ID': 'caller-controlled'}).json()
        second = client.get('/api/v1/proxies').json()
        assert first['request_id'] != 'caller-controlled'
        assert first['request_id'] != second['request_id']
        response = client.get('/legacy')
        assert response.status_code == 400
        assert response.json() == {'detail': 'legacy detail'}


@pytest.mark.parametrize('failure', [ConnectionError('private-database-password'), TimeoutError('private-database-password'), asyncpg.PostgresError('private-database-password')])
def test_database_failures_are_safe_enveloped_errors(setup, failure):
    app, conn, _ = setup
    conn.fetch.side_effect = failure
    with TestClient(app) as client:
        response = client.get('/api/v1/telegram/accounts')
    assert response.status_code == 503
    body = response.json()
    assert body['success'] is False
    assert body['error']['code'] == 'DATABASE_UNAVAILABLE'
    assert body['error']['retryable'] is True
    UUID(body['request_id'])
    assert 'private-database-password' not in response.text
