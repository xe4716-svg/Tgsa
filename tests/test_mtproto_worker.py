from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.workers import mtproto
from app.telegram.gateway_runtime import GatewayResult,GatewayCloseUncertain


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('reason,code',[('STOPPED',0),('FLOOD_WAIT',2),('WAIT_PERSIST_FAILED',2)])
async def test_worker_closes_pool_and_does_not_retry(monkeypatch,capsys,reason,code):
    closed=[];calls=[]
    @asynccontextmanager
    async def pool(*args,**kwargs):
        try:yield object()
        finally:closed.append(True)
    class Runner:
        def __init__(self,*args,**kwargs):pass
        async def run(self,*args):calls.append(True);return GatewayResult(reason,120)
    monkeypatch.setattr(mtproto.asyncpg,'create_pool',pool)
    monkeypatch.setattr(mtproto,'configure_worker_tracing',lambda **kwargs:None)
    monkeypatch.setattr(mtproto,'GatewayRunner',Runner)
    monkeypatch.setattr(mtproto.SessionVault,'from_base64',lambda *args:object())
    assert await mtproto.run(uuid4(),uuid4())==code
    assert len(calls)==1 and closed==[True]
    assert reason in capsys.readouterr().out


@pytest.mark.anyio
async def test_worker_sanitizes_database_failures(monkeypatch,capsys):
    def fail(*args,**kwargs):raise OSError('private-password-and-host')
    monkeypatch.setattr(mtproto.asyncpg,'create_pool',fail)
    monkeypatch.setattr(mtproto,'configure_worker_tracing',lambda **kwargs:None)
    monkeypatch.setattr(mtproto.SessionVault,'from_base64',lambda *args:object())
    assert await mtproto.run(uuid4(),uuid4())==2
    output=capsys.readouterr().out
    assert 'GATEWAY_FAILED' in output and 'private-password' not in output


def test_worker_requires_explicit_account_selection():
    with pytest.raises(SystemExit):mtproto.main([])


@pytest.mark.anyio
async def test_uncertain_disconnect_is_fatal_and_sanitized(monkeypatch,capsys):
    @asynccontextmanager
    async def pool(*args,**kwargs):yield object()
    class Runner:
        def __init__(self,*args,**kwargs):pass
        async def run(self,*args):raise GatewayCloseUncertain('private connection detail')
    monkeypatch.setattr(mtproto.asyncpg,'create_pool',pool)
    monkeypatch.setattr(mtproto,'configure_worker_tracing',lambda **kwargs:None)
    monkeypatch.setattr(mtproto,'GatewayRunner',Runner)
    monkeypatch.setattr(mtproto.SessionVault,'from_base64',lambda *args:object())
    assert await mtproto.run(uuid4(),uuid4())==3
    output=capsys.readouterr().out
    assert 'DISCONNECT_UNCERTAIN' in output and 'private connection' not in output
