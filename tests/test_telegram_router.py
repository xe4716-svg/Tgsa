"""Isolated route and coordinator regressions; no live Telegram traffic."""
import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.config import settings
from app.telegram.login_manager import PendingLogin, TelegramLoginManager
from app.telegram.login_router import router


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def env(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr('app.telegram.login_router._record_consent', AsyncMock())
    monkeypatch.setattr('app.telegram.login_router.finish_authorized_account', AsyncMock(return_value=True))
    manager = TelegramLoginManager()
    app.state.telegram_login_manager = manager
    principal = AuthPrincipal(uuid4(), uuid4(), uuid4(), 'OWNER', 'test@example.test', False)
    app.dependency_overrides[get_current_principal] = lambda: principal
    monkeypatch.setattr('app.telegram.login_router.settings', SimpleNamespace(session_kek_base64='eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=', session_kek_id='test-key'))
    return app, manager, principal


def add_pending(manager, principal, **kwargs):
    pending = PendingLogin(
        login_id='opaque', tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
        method='QR', client=AsyncMock(), created_at=time.time(), expires_at=time.time()+60,
        status='AUTHORIZED', session_string='test-session-secret', account={'telegram_user_id': 123},
        connection_mode='DIRECT', **kwargs,
    )
    manager._pending[pending.login_id] = pending
    return pending


@pytest.mark.anyio
async def test_login_requires_authentication(env):
    app, _, _ = env
    app.dependency_overrides.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/telegram/login/qr', json={'connection_mode':'DIRECT'})
        assert response.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize('data', [
    {'connection_mode':'DIRECT'},
    {'connection_mode':'DIRECT','notice_version':'v1.0','risk_acknowledged':False},
    {'connection_mode':'direct','notice_version':'v1.0','risk_acknowledged':True},
    {'connection_mode':'DIRECT','notice_version':'old','risk_acknowledged':True},
])
async def test_no_network_before_explicit_consent(env, monkeypatch, data):
    app, manager, _ = env
    start = AsyncMock()
    monkeypatch.setattr(manager, 'start_qr', start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/telegram/login/qr', json=data)).status_code == 422
    start.assert_not_awaited()


@pytest.mark.anyio
async def test_direct_rejects_proxy_and_valid_choice_is_forwarded(env, monkeypatch):
    app, manager, principal = env
    start = AsyncMock(return_value={'status':'QR_PENDING'})
    monkeypatch.setattr(manager, 'start_qr', start)
    body = {'connection_mode':'DIRECT','notice_version':'v1.0','risk_acknowledged':True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/telegram/login/qr', json={**body,'proxy_profile_id':str(uuid4())})).status_code == 400
        start.assert_not_awaited()
        assert (await client.post('/telegram/login/qr', json=body)).status_code == 200
    start.assert_awaited_once_with(str(principal.tenant_id), str(principal.user_id), connection_mode='DIRECT', proxy_profile=None, notice_version='v1.0')


@pytest.mark.anyio
async def test_concurrent_consume_is_once_and_disconnect_failure_does_not_repeat(env, monkeypatch):
    app, manager, principal = env
    pending = add_pending(manager, principal)
    pending.client.disconnect.side_effect = OSError('private network details')
    account_id = uuid4()
    persist = AsyncMock(return_value=account_id)
    monkeypatch.setattr('app.telegram.login_router.persist_authorized_account', persist)
    app.state.db_pool = object()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        responses = await asyncio.gather(*[client.post('/telegram/login/opaque/consume', json={'notice_version':'v1.0'}) for _ in range(3)])
    assert all(r.status_code == 200 and r.json() == {'account_id':str(account_id)} for r in responses)
    persist.assert_awaited_once()
    assert pending.session_string is None
    assert pending.status == 'CONSUMED'


@pytest.mark.anyio
async def test_failure_preserves_authorization_and_hides_database_details(env, monkeypatch):
    import asyncpg
    app, manager, principal = env
    pending = add_pending(manager, principal)
    persist = AsyncMock(side_effect=asyncpg.PostgresError('session=secret database=private'))
    monkeypatch.setattr('app.telegram.login_router.persist_authorized_account', persist)
    app.state.db_pool = object()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/telegram/login/opaque/consume', json={'notice_version':'v1.0'})
        assert response.status_code == 503
        assert 'private' not in response.text and 'secret' not in response.text
    assert pending.session_string == 'test-session-secret'
    assert pending.status == 'AUTHORIZED'


@pytest.mark.anyio
async def test_cross_tenant_flow_is_hidden(env):
    app, manager, principal = env
    pending = add_pending(manager, principal)
    pending.tenant_id = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/telegram/login/opaque/status')).status_code == 404
        assert (await client.post('/telegram/login/opaque/consume', json={'notice_version':'v1.0'})).status_code == 404


@pytest.mark.anyio
async def test_phone_code_can_be_retried_after_rejection(env):
    _, manager, principal = env
    pending = add_pending(manager, principal)
    pending.method = 'PHONE'
    pending.status = 'CODE_REJECTED'
    pending.client.sign_in.return_value = SimpleNamespace(id=123)
    pending.client.session.save = lambda: 'synthetic-session'
    result = await manager.submit_phone_code('opaque', str(principal.tenant_id), str(principal.user_id), '12345')
    assert result['status'] == 'AUTHORIZED'


@pytest.mark.anyio
async def test_shutdown_awaits_tasks_and_clears_sessions(env):
    _, manager, principal = env
    pending = add_pending(manager, principal)
    pending.wait_task = asyncio.create_task(asyncio.sleep(10))
    await manager.shutdown()
    await asyncio.sleep(0)
    assert pending.wait_task.done()
    assert not manager._pending
    assert pending.session_string is None
    pending.client.disconnect.assert_awaited_once()


@pytest.mark.anyio
async def test_expired_cleanup_survives_one_disconnect_failure(env):
    _, manager, principal = env
    pending = add_pending(manager, principal)
    pending.expires_at = time.time()-1
    pending.client.disconnect.side_effect = OSError('synthetic failure')
    assert await manager.cleanup_expired() == 1
    assert not manager._pending and pending.session_string is None


@pytest.mark.anyio
async def test_proxy_reader_uses_existing_encrypted_columns(env):
    from app.telegram.login_router import _resolve_proxy_profile
    from app.security.proxy_vault import ProxyVault
    app, _, principal = env
    proxy_id = uuid4()
    blob = ProxyVault(b'x'*32, 'test-key').encode_credentials(principal.tenant_id, proxy_id, {'password':'private-password'})
    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield
        async def execute(self, query, *args):
            assert args == (str(principal.tenant_id),)
        async def fetchrow(self, query, *args):
            assert 'credentials_ciphertext' in query and 'username,' not in query
            assert args == (principal.tenant_id, proxy_id)
            return dict(id=proxy_id, proxy_type='SOCKS5', host='proxy.example.test', port=1080,
                        credentials_ciphertext=blob, secret_version='test-key', verification_status='VERIFIED', status='ACTIVE')
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()
    app.state.db_pool = Pool()
    profile = await _resolve_proxy_profile(SimpleNamespace(app=app), 'PROXY', proxy_id, principal.tenant_id)
    assert profile['password'] == 'private-password'
    assert 'credentials_ciphertext' not in profile


@pytest.mark.anyio
async def test_viewer_cannot_start_authorization(env, monkeypatch):
    app, manager, principal = env
    app.dependency_overrides[get_current_principal] = lambda: AuthPrincipal(principal.user_id,principal.tenant_id,principal.family_id,'VIEWER',principal.email,False)
    start = AsyncMock()
    monkeypatch.setattr(manager, 'start_qr', start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/telegram/login/qr', json={'connection_mode':'DIRECT','notice_version':'v1.0','risk_acknowledged':True})
    assert response.status_code == 403
    start.assert_not_awaited()


@pytest.mark.anyio
async def test_invalid_password_is_not_echoed_in_validation_errors(env):
    app, _, _ = env
    secret = 'do-not-echo-password-'*100
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/telegram/login/2fa', json={'login_id':'opaque','password':secret})
    assert response.status_code == 422 and secret not in response.text


@pytest.mark.anyio
async def test_commit_finishing_after_ttl_still_records_consumed_result(env, monkeypatch):
    app, manager, principal = env
    pending = add_pending(manager, principal)
    identifier = uuid4()
    async def persist(*args, **kwargs):
        pending.expires_at = time.time()-1
        return identifier
    monkeypatch.setattr('app.telegram.login_router.persist_authorized_account', persist)
    app.state.db_pool = object()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/telegram/login/opaque/consume', json={'notice_version':'v1.0'})
    assert response.status_code == 200 and response.json()=={'account_id':str(identifier)}
    assert pending.status == 'CONSUMED' and pending.session_string is None


@pytest.mark.anyio
async def test_login_capacity_and_telegram_wait_map_to_retry_after(env, monkeypatch):
    from app.telegram.login_manager import LoginCapacityExceeded, LoginThrottled
    app, manager, _ = env
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        for error in [LoginCapacityExceeded(30),LoginThrottled(90)]:
            start=AsyncMock(side_effect=error)
            monkeypatch.setattr(manager,'start_qr',start)
            result=await client.post('/telegram/login/qr',json={'connection_mode':'DIRECT','notice_version':'v1.0','risk_acknowledged':True})
            assert result.status_code==429 and result.headers['retry-after']==str(error.retry_after)


@pytest.mark.anyio
async def test_failed_disconnect_never_activates_and_retry_only_finishes_handoff(env,monkeypatch):
    app,manager,principal=env
    pending=add_pending(manager,principal)
    pending.client.disconnect.side_effect=[OSError('synthetic'),None]
    identifier=uuid4()
    persist=AsyncMock(return_value=identifier);finish=AsyncMock(return_value=True)
    monkeypatch.setattr('app.telegram.login_router.persist_authorized_account',persist)
    monkeypatch.setattr('app.telegram.login_router.finish_authorized_account',finish)
    app.state.db_pool=object()
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        first=await client.post('/telegram/login/opaque/consume',json={'notice_version':'v1.0'})
        assert first.status_code==200 and not pending.connection_closed
        finish.assert_not_awaited()
        second=await client.post('/telegram/login/opaque/consume',json={'notice_version':'v1.0'})
        assert second.status_code==200 and pending.connection_closed
    assert first.json()==second.json()=={'account_id':str(identifier)}
    persist.assert_awaited_once();finish.assert_awaited_once()


@pytest.mark.anyio
async def test_activation_database_failure_retries_without_repeating_account_or_disconnect(env,monkeypatch):
    import asyncpg
    app,manager,principal=env
    pending=add_pending(manager,principal)
    identifier=uuid4()
    persist=AsyncMock(return_value=identifier)
    finish=AsyncMock(side_effect=[asyncpg.PostgresError('private db credential'),True])
    monkeypatch.setattr('app.telegram.login_router.persist_authorized_account',persist)
    monkeypatch.setattr('app.telegram.login_router.finish_authorized_account',finish)
    app.state.db_pool=object()
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        first=await client.post('/telegram/login/opaque/consume',json={'notice_version':'v1.0'})
        assert first.status_code==503 and 'private db' not in first.text
        second=await client.post('/telegram/login/opaque/consume',json={'notice_version':'v1.0'})
        assert second.status_code==200
    persist.assert_awaited_once();pending.client.disconnect.assert_awaited_once()
    assert finish.await_count==2 and pending.session_string is None
