import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from telethon.errors import FloodWaitError

from app.telegram.gateway_material import AccountMaterial
from app.telegram.gateway_leases import AccountLease
from app.telegram.gateway_runtime import GatewayRunner,GatewayCloseUncertain


@pytest.fixture
def anyio_backend():return 'asyncio'


class Leases:
    def __init__(self):
        self.lease=AccountLease(uuid4(),uuid4(),'test',1)
        self.allowed=True;self.renewed=True;self.calls=[];self.deferred=True
        self.conn=SimpleNamespace(execute=AsyncMock())
    async def acquire(self,*args):
        self.calls.append('acquire');return self.lease if self.allowed else None
    async def renew(self,*args):
        self.calls.append('renew')
        if isinstance(self.renewed,Exception):raise self.renewed
        return self.renewed
    async def release(self,*args):self.calls.append('release');return True
    async def defer(self,*args):self.calls.append('defer');return self.deferred
    @asynccontextmanager
    async def fenced_transaction(self,*args):yield self.conn


async def setup(**options):
    leases=Leases()
    material=AccountMaterial(123,'DIRECT','revision-1','private-session',None)
    revision=['revision-1']
    async def loader(conn,lease,vault,*,decrypt):return material if decrypt else revision[0]
    disconnected=asyncio.get_running_loop().create_future()
    client=SimpleNamespace(connect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(id=123)),disconnect=AsyncMock(),disconnected=disconnected)
    def create(session,**kwargs):
        assert session=='private-session' and kwargs['managed'] is True
        assert kwargs['connection_mode']=='DIRECT' and kwargs['proxy_profile'] is None
        leases.calls.append('create');return client
    async def close():leases.calls.append('disconnect')
    client.disconnect.side_effect=close
    runner=GatewayRunner(None,object(),'test',ttl=6,heartbeat=.01,control_timeout=.2,rpc_timeout=.3,close_timeout=.2,
                         leases=leases,loader=loader,client_factory=create,**options)
    return runner,leases,client,revision


@pytest.mark.anyio
async def test_clean_stop_disconnects_before_releasing_and_does_not_cancel_telethon_future():
    runner,leases,client,_=await setup();stop=asyncio.Event();ready=asyncio.Event()
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,stop,ready=ready))
    await asyncio.wait_for(ready.wait(),1);stop.set()
    assert (await task).reason=='STOPPED'
    assert leases.calls.index('disconnect')<leases.calls.index('release')
    assert not client.disconnected.cancelled()
    client.get_me.assert_awaited_once()


@pytest.mark.anyio
async def test_busy_account_never_constructs_client():
    runner,leases,client,_=await setup();leases.allowed=False
    result=await runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event())
    assert result.reason=='ACCOUNT_UNAVAILABLE' and leases.calls==['acquire']
    client.connect.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('failure',[False,OSError('private database address')])
async def test_renewal_failure_cancels_inflight_rpc_and_disconnects(failure):
    runner,leases,client,_=await setup();entered=asyncio.Event();cancelled=asyncio.Event()
    async def read():
        entered.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    client.get_me.side_effect=read
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event()))
    await asyncio.wait_for(entered.wait(),1);leases.renewed=failure
    result=await asyncio.wait_for(task,1)
    assert result.reason=='OWNERSHIP_LOST' and cancelled.is_set()
    client.disconnect.assert_awaited_once()


@pytest.mark.anyio
async def test_changed_material_stops_ready_connection():
    runner,leases,client,revision=await setup();ready=asyncio.Event()
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event(),ready=ready))
    await asyncio.wait_for(ready.wait(),1);revision[0]='changed'
    assert (await asyncio.wait_for(task,1)).reason=='MATERIAL_CHANGED'
    client.disconnect.assert_awaited_once()


@pytest.mark.anyio
async def test_connection_close_does_not_automatically_reconnect():
    runner,leases,client,_=await setup();ready=asyncio.Event()
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event(),ready=ready))
    await asyncio.wait_for(ready.wait(),1);client.disconnected.set_result(None)
    assert (await asyncio.wait_for(task,1)).reason=='CONNECTION_CLOSED'
    client.connect.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize('reason',['AUTHORIZATION_INVALID','IDENTITY_MISMATCH'])
async def test_invalid_identity_never_becomes_ready(reason):
    runner,leases,client,_=await setup();ready=asyncio.Event()
    if reason=='AUTHORIZATION_INVALID':client.is_user_authorized.return_value=False
    else:client.get_me.return_value=SimpleNamespace(id=999)
    assert (await runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event(),ready=ready)).reason==reason
    assert not ready.is_set()
    client.disconnect.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize('persisted',[True,False])
async def test_flood_wait_holds_deadline_and_never_releases_or_retries(persisted):
    runner,leases,client,_=await setup();leases.deferred=persisted
    client.get_me.side_effect=FloodWaitError(request=None,capture=120)
    result=await runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event())
    assert result.reason==('FLOOD_WAIT' if persisted else 'WAIT_PERSIST_FAILED') and result.retry_after==120
    assert 'defer' in leases.calls and 'release' not in leases.calls
    client.get_me.assert_awaited_once();client.disconnect.assert_awaited_once()


@pytest.mark.anyio
async def test_failed_disconnect_quarantines_current_account_and_does_not_release():
    runner,leases,client,_=await setup();stop=asyncio.Event();ready=asyncio.Event()
    client.disconnect.side_effect=OSError('private connection diagnostic')
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,stop,ready=ready))
    await asyncio.wait_for(ready.wait(),1);stop.set()
    with pytest.raises(GatewayCloseUncertain,match='could not be confirmed'):await task
    assert 'release' not in leases.calls
    statements=[call.args[0] for call in leases.conn.execute.await_args_list]
    assert any("PAUSED" in sql for sql in statements)
    assert any("account_security_events" in sql for sql in statements)


@pytest.mark.anyio
async def test_external_cancellation_still_closes_then_releases():
    runner,leases,client,_=await setup();ready=asyncio.Event()
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event(),ready=ready))
    await asyncio.wait_for(ready.wait(),1);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert leases.calls.index('disconnect')<leases.calls.index('release')


@pytest.mark.anyio
async def test_stop_interrupts_pending_connect():
    runner,leases,client,_=await setup();stop=asyncio.Event();entered=asyncio.Event()
    async def connect():entered.set();await asyncio.Event().wait()
    client.connect.side_effect=connect
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,stop))
    await asyncio.wait_for(entered.wait(),1);stop.set()
    assert (await asyncio.wait_for(task,1)).reason=='STOPPED'
    client.disconnect.assert_awaited_once()


def test_material_repr_cannot_expose_session_or_proxy_password():
    material=AccountMaterial(123,'PROXY','digest','secret-session',{'password':'secret-password'})
    assert 'secret-session' not in repr(material) and 'secret-password' not in repr(material)


@pytest.mark.anyio
async def test_stale_route_is_refused_before_client_creation():
    runner,leases,client,_=await setup()
    leases.conn.fetchval=AsyncMock(return_value=False)
    result=await runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event(),route_generation=7)
    assert result.reason=='OWNERSHIP_LOST'
    assert 'create' not in leases.calls
    client.connect.assert_not_awaited()


@pytest.mark.anyio
async def test_disabling_current_route_disconnects_the_ready_client():
    runner,leases,client,_=await setup();ready=asyncio.Event()
    leases.conn.fetchval=AsyncMock(return_value=True)
    task=asyncio.create_task(runner.run(leases.lease.tenant_id,leases.lease.account_id,asyncio.Event(),ready=ready,route_generation=7))
    await asyncio.wait_for(ready.wait(),1)
    leases.conn.fetchval.return_value=False
    assert (await asyncio.wait_for(task,1)).reason=='MATERIAL_UNAVAILABLE'
    client.disconnect.assert_awaited_once()
