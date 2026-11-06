import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.telegram.gateway_routes import GatewayRoute
from app.telegram.gateway_runtime import GatewayResult, GatewayCloseUncertain
from app.telegram.gateway_supervisor import GatewaySupervisor


@pytest.fixture
def anyio_backend():return 'asyncio'


class Routes:
    def __init__(self, tenants, count=3):
        self.rows={t:[GatewayRoute(t,uuid4(),1) for _ in range(count)] for t in tenants}
        self.pause=AsyncMock(side_effect=self.paused)

    async def enabled(self, tenant):return self.rows[tenant][:]

    async def paused(self, route, reason):
        if route in self.rows[route.tenant_id]:
            self.rows[route.tenant_id].remove(route)
            return True
        return False


class Factory:
    def __init__(self):
        self.calls=[];self.results={};self.exits=[];self.traceparents=[]

    def __call__(self):return self

    async def run(self, tenant, account, stop, *, route_generation, traceparent=None):
        route=GatewayRoute(tenant,account,route_generation)
        self.calls.append(route)
        self.traceparents.append(traceparent)
        try:
            if account in self.results:
                result=self.results[account]
                if isinstance(result,Exception):raise result
                return GatewayResult(result)
            await stop.wait()
            return GatewayResult('STOPPED')
        finally:self.exits.append(route)


@pytest.mark.anyio
async def test_capacity_is_shared_fairly_and_shutdown_preserves_routes():
    tenants=[uuid4() for _ in range(3)];store=Routes(tenants);factory=Factory()
    supervisor=GatewaySupervisor(store,factory,tenants,capacity=3,per_tenant=2)
    await supervisor.step();await asyncio.sleep(0)
    assert len(supervisor.running)==3 and {r.tenant_id for r in factory.calls}==set(tenants)
    await supervisor.step();assert len(factory.calls)==3
    await supervisor.close()
    assert len(factory.exits)==3 and not supervisor.running
    store.pause.assert_not_awaited()


@pytest.mark.anyio
async def test_new_generation_waits_for_old_client_cleanup():
    tenant=uuid4();store=Routes([tenant],1);factory=Factory()
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=1,per_tenant=1)
    old=store.rows[tenant][0]
    await supervisor.step();await asyncio.sleep(0)
    store.rows[tenant][0]=replace(old,generation=2)
    await supervisor.step()
    assert len(factory.calls)==1
    await asyncio.sleep(0)
    await supervisor.step();await asyncio.sleep(0)
    assert factory.calls==[old,replace(old,generation=2)] and factory.exits==[old]
    await supervisor.close();store.pause.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('reason',['FLOOD_WAIT','WAIT_PERSIST_FAILED','CONNECTION_CLOSED','MATERIAL_CHANGED','AUTHORIZATION_INVALID','STOPPED'])
async def test_failed_account_is_paused_without_reconnect(reason):
    tenant=uuid4();store=Routes([tenant],1);factory=Factory();route=store.rows[tenant][0]
    factory.results[route.account_id]=reason
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=1,per_tenant=1)
    await supervisor.step();await asyncio.sleep(0)
    await supervisor.step();await supervisor.step()
    assert factory.calls==[route]
    store.pause.assert_awaited_once_with(route,'UNEXPECTED_STOP' if reason=='STOPPED' else reason)
    await supervisor.close()


@pytest.mark.anyio
async def test_busy_account_only_retries_database_after_cooldown_and_frees_capacity():
    tenant=uuid4();store=Routes([tenant],2);factory=Factory();now=[10]
    first=store.rows[tenant][0];factory.results[first.account_id]='ACCOUNT_UNAVAILABLE'
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=1,per_tenant=1,clock=lambda:now[0])
    await supervisor.step();await asyncio.sleep(0)
    await supervisor.step();await asyncio.sleep(0)
    assert len(factory.calls)==2 and factory.calls[1]!=first
    for _ in range(5):await supervisor.step()
    assert len(factory.calls)==2
    store.rows[tenant]=[first]
    await supervisor.step();await asyncio.sleep(0);await supervisor.step()
    assert len(factory.calls)==2
    now[0]=41
    await supervisor.step();await asyncio.sleep(0)
    assert factory.calls[-1]==first and len(factory.calls)==3
    store.pause.assert_not_awaited();await supervisor.close()


@pytest.mark.anyio
async def test_database_failure_stops_all_connections_without_leaking_tasks():
    tenant=uuid4();store=Routes([tenant],2);factory=Factory()
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=2,per_tenant=2,poll_interval=.001)
    original=store.enabled
    async def read(t):
        if factory.calls:raise OSError('private database details')
        return await original(t)
    store.enabled=read
    with pytest.raises(OSError):await supervisor.serve(asyncio.Event())
    assert len(factory.exits)==2 and not supervisor.running


@pytest.mark.anyio
async def test_uncertain_disconnect_stops_other_accounts_and_propagates_fatal_exit():
    tenant=uuid4();store=Routes([tenant],2);factory=Factory();route=store.rows[tenant][0]
    factory.results[route.account_id]=GatewayCloseUncertain('synthetic')
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=2,per_tenant=2,poll_interval=.001)
    with pytest.raises(GatewayCloseUncertain):await supervisor.serve(asyncio.Event())
    assert len(factory.calls)==2 and len(factory.exits)==2 and not supervisor.running
    store.pause.assert_awaited_once_with(route,'DISCONNECT_UNCERTAIN')


@pytest.mark.anyio
async def test_failure_during_shutdown_is_still_recorded():
    tenant=uuid4();store=Routes([tenant],1);factory=Factory();route=store.rows[tenant][0]
    factory.results[route.account_id]='CONNECTION_CLOSED'
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=1,per_tenant=1)
    await supervisor.step();await asyncio.sleep(0);await supervisor.close()
    store.pause.assert_awaited_once_with(route,'CONNECTION_CLOSED')


def test_supervisor_requires_bounded_explicit_tenants():
    for tenants,options in [([] ,{}),([uuid4()],{'capacity':0}),([uuid4()],{'per_tenant':30}),
                            ([uuid4() for _ in range(101)],{})]:
        with pytest.raises(ValueError):GatewaySupervisor(None,None,tenants,**options)


@pytest.mark.anyio
async def test_pause_storage_failure_cannot_hide_uncertain_disconnect_or_skip_other_cleanup():
    tenant=uuid4();store=Routes([tenant],2);factory=Factory()
    for route in store.rows[tenant]:factory.results[route.account_id]=GatewayCloseUncertain('synthetic')
    store.pause.side_effect=OSError('private database diagnostic')
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=2,per_tenant=2,poll_interval=.001)
    with pytest.raises(GatewayCloseUncertain):await supervisor.serve(asyncio.Event())
    assert len(factory.exits)==2 and not supervisor.running
    assert store.pause.await_count==2



@pytest.mark.anyio
async def test_route_trace_context_is_forwarded_once_without_changing_route_identity():
    tenant=uuid4();store=Routes([tenant],1);factory=Factory()
    original=store.rows[tenant][0]
    traced=replace(original,traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    store.rows[tenant][0]=traced
    supervisor=GatewaySupervisor(store,factory,[tenant],capacity=1,per_tenant=1)
    await supervisor.step();await asyncio.sleep(0)
    assert factory.calls==[original]
    assert factory.traceparents==[traced.traceparent]
    # A later poll may no longer carry trace metadata, but must not restart
    # the same route generation or close its healthy connection.
    untraced=replace(traced,traceparent=None)
    assert traced==untraced
    store.rows[tenant][0]=untraced
    await supervisor.step()
    assert factory.calls==[original]
    await supervisor.close()
