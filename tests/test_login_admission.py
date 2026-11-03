import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError, PhoneCodeInvalidError, PasswordHashInvalidError

from app.telegram.login_manager import TelegramLoginManager, LoginCapacityExceeded, LoginThrottled


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def client():
    return SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash='synthetic-hash')),
        sign_in=AsyncMock(return_value=SimpleNamespace(id=123)),session=SimpleNamespace(save=lambda:'synthetic-session'))


@pytest.mark.anyio
@pytest.mark.parametrize('limits,owner', [({'max_pending':1},('other','other')),({'max_per_tenant':1},('tenant','other')),({'max_per_user':1},('tenant','user'))])
async def test_pending_and_inflight_starts_share_admission_limit(monkeypatch, limits, owner):
    manager=TelegramLoginManager(**limits)
    fake=client();entered=asyncio.Event();release=asyncio.Event()
    async def connect():
        entered.set();await release.wait()
    fake.connect.side_effect=connect
    factory=lambda **kwargs:fake
    monkeypatch.setattr('app.telegram.login_manager.create_client',factory)
    first=asyncio.create_task(manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT'))
    await entered.wait()
    with pytest.raises(LoginCapacityExceeded):
        await manager.start_phone(*owner,'+12345678901',connection_mode='DIRECT')
    release.set();await first
    with pytest.raises(LoginCapacityExceeded):
        await manager.start_phone(*owner,'+12345678901',connection_mode='DIRECT')
    fake.connect.assert_awaited_once()
    await manager.shutdown()


@pytest.mark.anyio
async def test_cancelled_start_disconnects_and_releases_capacity(monkeypatch):
    manager=TelegramLoginManager(max_pending=1)
    fake=client();entered=asyncio.Event()
    async def connect():
        entered.set();await asyncio.Event().wait()
    fake.connect.side_effect=connect
    monkeypatch.setattr('app.telegram.login_manager.create_client',lambda **kwargs:fake)
    task=asyncio.create_task(manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT'))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not manager._starting and not manager._pending
    fake.disconnect.assert_awaited_once()


@pytest.mark.anyio
async def test_shutdown_cancels_inflight_network_and_rejects_new_starts(monkeypatch):
    manager=TelegramLoginManager()
    fake=client();entered=asyncio.Event()
    async def connect():
        entered.set();await asyncio.Event().wait()
    fake.connect.side_effect=connect
    monkeypatch.setattr('app.telegram.login_manager.create_client',lambda **kwargs:fake)
    task=asyncio.create_task(manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT'))
    await entered.wait();await manager.shutdown()
    assert task.cancelled() and not manager._pending and not manager._starting
    fake.disconnect.assert_awaited_once()
    with pytest.raises(LoginCapacityExceeded):
        await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')


@pytest.mark.anyio
async def test_start_flood_wait_blocks_new_network_calls_until_server_deadline(monkeypatch):
    now=[100.0]
    manager=TelegramLoginManager()
    manager._clock=lambda:now[0]
    fake=client();fake.send_code_request.side_effect=FloodWaitError(request=None,capture=45)
    monkeypatch.setattr('app.telegram.login_manager.create_client',lambda **kwargs:fake)
    with pytest.raises(LoginThrottled) as error:
        await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')
    assert error.value.retry_after==45
    now[0]=130
    with pytest.raises(LoginThrottled) as error:
        await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')
    assert error.value.retry_after==15
    fake.connect.assert_awaited_once()
    now[0]=146
    fake.send_code_request.side_effect=None
    await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')
    assert fake.connect.await_count==2
    await manager.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize('password',[False,True])
async def test_invalid_credentials_have_bounded_attempts(monkeypatch,password):
    manager=TelegramLoginManager(max_attempts=2)
    fake=client()
    monkeypatch.setattr('app.telegram.login_manager.create_client',lambda **kwargs:fake)
    result=await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')
    pending=manager._pending[result['login_id']]
    if password:
        pending.status='PASSWORD_REQUIRED'
        fake.sign_in.side_effect=PasswordHashInvalidError(request=None)
        operation=manager.submit_2fa_password
    else:
        fake.sign_in.side_effect=PhoneCodeInvalidError(request=None)
        operation=manager.submit_phone_code
    for _ in range(2):await operation(result['login_id'],'tenant','user','wrong')
    assert pending.status=='FAILED' and pending.error_code=='LOGIN_ATTEMPTS_EXHAUSTED'
    with pytest.raises(ValueError):await operation(result['login_id'],'tenant','user','wrong')
    assert fake.sign_in.await_count==2 and pending.session_string is None
    fake.disconnect.assert_awaited_once()
    await manager.shutdown()


@pytest.mark.anyio
async def test_network_failure_disconnects_and_has_distinct_service_error(monkeypatch):
    from app.telegram.login_manager import LoginTransportUnavailable
    manager=TelegramLoginManager()
    fake=client();fake.connect.side_effect=OSError('private-proxy-password')
    monkeypatch.setattr('app.telegram.login_manager.create_client',lambda **kwargs:fake)
    with pytest.raises(LoginTransportUnavailable) as error:
        await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')
    assert 'private-proxy-password' not in str(error.value)
    fake.disconnect.assert_awaited_once()
    assert not manager._starting and not manager._pending


@pytest.mark.anyio
async def test_code_flood_preserves_flow_and_blocks_immediate_retry(monkeypatch):
    manager=TelegramLoginManager();clock=[1.0];manager._clock=lambda:clock[0]
    fake=client()
    monkeypatch.setattr('app.telegram.login_manager.create_client',lambda **kwargs:fake)
    result=await manager.start_phone('tenant','user','+12345678901',connection_mode='DIRECT')
    fake.sign_in.side_effect=FloodWaitError(request=None,capture=60)
    for _ in range(2):
        with pytest.raises(LoginThrottled):
            await manager.submit_phone_code(result['login_id'],'tenant','user','12345')
    fake.sign_in.assert_awaited_once()
    assert manager.get_status(result['login_id'],'tenant','user')['retry_after']==60
    clock[0]=62;fake.sign_in.side_effect=None
    result=await manager.submit_phone_code(result['login_id'],'tenant','user','12345')
    assert result['status']=='AUTHORIZED'
    await manager.shutdown()


def test_client_does_not_hide_flood_wait_in_internal_sleep(monkeypatch):
    from app.telegram import client_factory
    from unittest.mock import Mock
    monkeypatch.setattr(client_factory,'settings',SimpleNamespace(telegram_api_id=123,telegram_api_hash='synthetic-app-hash'))
    factory=Mock()
    monkeypatch.setattr('telethon.TelegramClient',factory)
    client_factory.create_client(connection_mode='DIRECT')
    assert factory.call_args.kwargs['flood_sleep_threshold']==0
