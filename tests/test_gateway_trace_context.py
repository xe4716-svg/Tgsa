from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider

from app.auth.dependencies import AuthPrincipal
from app.observability import current_traceparent
from app.telegram import gateway_routes
from app.telegram.gateway_routes import GatewayRoute, GatewayRoutes


class ChangeConnection:
    def __init__(self, tenant_id, account_id):
        self.tenant_id=tenant_id
        self.account_id=account_id
        self.insert_args=None

    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    def transaction(self): return self

    async def fetchrow(self, query, *args):
        if "SELECT id FROM tenants" in query:
            return {"id":self.tenant_id}
        if "SELECT status,security_state FROM telegram_accounts" in query:
            return {"status":"AUTHORIZED","security_state":"OK"}
        if "SELECT generation FROM account_gateway_routes" in query:
            return None
        if "INSERT INTO account_gateway_routes" in query:
            self.insert_args=args
            return {
                "account_id":self.account_id,
                "enabled":args[2],
                "generation":1,
                "last_reason":None,
            }
        raise AssertionError("Unexpected fetchrow query")

    async def fetchval(self, query, *args):
        if "count(*) FROM account_gateway_routes" in query:
            return 0
        raise AssertionError("Unexpected fetchval query")

    async def execute(self, query, *args):
        return "OK"


class ChangePool:
    def __init__(self, conn): self.conn=conn
    def acquire(self): return self.conn


class EnabledConnection:
    def __init__(self, tenant_id, account_id, traceparent):
        self.tenant_id=tenant_id
        self.account_id=account_id
        self.traceparent=traceparent
        self.updates=0

    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    def transaction(self): return self

    async def execute(self, query, *args):
        if "UPDATE account_gateway_routes SET traceparent=NULL" in query:
            assert args[:3]==(self.tenant_id,self.account_id,1)
            assert args[3]==self.traceparent
            self.traceparent=None
            self.updates+=1
        return "OK"

    async def fetch(self, query, *args):
        return [{
            "account_id":self.account_id,
            "generation":1,
            "traceparent":self.traceparent,
        }]


class EnabledPool:
    def __init__(self, conn): self.conn=conn
    def acquire(self): return self.conn


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_route_change_persists_only_the_active_w3c_traceparent(monkeypatch):
    tenant_id,account_id,user_id,family_id=uuid4(),uuid4(),uuid4(),uuid4()
    principal=AuthPrincipal(user_id,tenant_id,family_id,"OWNER","owner@example.test",False)
    conn=ChangeConnection(tenant_id,account_id)
    monkeypatch.setattr(gateway_routes,"lock_login_authority",lambda *args: _noop())
    provider=TracerProvider()
    try:
        with provider.get_tracer("gateway-routes-test").start_as_current_span("api.gateway.route.change"):
            expected=current_traceparent()
            result=await GatewayRoutes(ChangePool(conn)).change(principal,account_id,True,0)
        assert conn.insert_args==(tenant_id,account_id,True,expected)
        assert result=={
            "account_id":account_id,
            "enabled":True,
            "generation":1,
            "last_reason":None,
        }
        assert "traceparent" not in result
    finally:
        provider.shutdown()


async def _noop():
    return None


@pytest.mark.anyio
async def test_enabled_routes_consume_traceparent_once_and_hide_it_from_repr():
    tenant_id,account_id=uuid4(),uuid4()
    traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    conn=EnabledConnection(tenant_id,account_id,traceparent)
    store=GatewayRoutes(EnabledPool(conn))

    first=await store.enabled(tenant_id)
    second=await store.enabled(tenant_id)

    assert len(first)==len(second)==1
    assert first[0].traceparent==traceparent
    assert second[0].traceparent is None
    assert first[0]==second[0]
    assert "4bf92f" not in repr(first[0])
    assert conn.updates==1
