from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.workers import gateway
from app.telegram.gateway_runtime import GatewayCloseUncertain


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('failure,code',[(None,0),(OSError('private database password'),2),(GatewayCloseUncertain('private socket'),3)])
async def test_gateway_worker_resource_cleanup_and_sanitized_exit(monkeypatch,capsys,failure,code):
    closed=[];restored=[];captured=[];runner_kwargs=[];shutdowns=[]
    tracer=object()
    provider=SimpleNamespace(get_tracer=lambda *args:tracer,shutdown=lambda:shutdowns.append(True))
    @asynccontextmanager
    async def pool(*args,**kwargs):
        try:yield object()
        finally:closed.append(True)
    class Supervisor:
        def __init__(self,store,factory,tenants,**kwargs):
            captured.append((tenants,kwargs))
            factory()
    class Runner:
        def __init__(self,*args,**kwargs):runner_kwargs.append(kwargs)
        async def serve(self,stop):
            if failure:raise failure
    monkeypatch.setattr(gateway.asyncpg,'create_pool',pool)
    monkeypatch.setattr(gateway,'install_signal_handlers',lambda stop:[lambda:restored.append(True)])
    monkeypatch.setattr(gateway,'configure_worker_tracing',lambda **kwargs:provider)
    monkeypatch.setattr(gateway,'GatewayRunner',Runner)
    monkeypatch.setattr(gateway.SessionVault,'from_base64',lambda *a:object())
    monkeypatch.setattr(gateway,'GatewaySupervisor',Supervisor)
    tenants=[uuid4(),uuid4()]
    assert await gateway.run(tenants,10,3)==code
    assert closed==[True] and restored==[True]
    assert captured==[(tenants,{'capacity':10,'per_tenant':3})]
    assert runner_kwargs==[{'command_tracer':tracer}]
    assert shutdowns==[True]
    assert 'private' not in capsys.readouterr().out


@pytest.mark.parametrize('args',[[],['--tenant',str(uuid4()),'--capacity','0'],['--tenant',str(uuid4()),'--per-tenant','21']])
def test_gateway_cli_rejects_unbounded_or_unassigned_configuration(args):
    with pytest.raises(SystemExit):gateway.main(args)


def test_gateway_cli_reads_only_explicit_environment_allowlist(monkeypatch):
    tenants=[uuid4(),uuid4()];calls=[]
    async def run(selected,capacity,per_tenant):calls.append(selected);return 0
    monkeypatch.setattr(gateway,'run',run)
    monkeypatch.setenv('GATEWAY_TENANT_IDS',','.join(map(str,tenants)))
    assert gateway.main(['--from-env'])==0 and calls==[tenants]
    monkeypatch.setenv('GATEWAY_TENANT_IDS','private-invalid-value')
    with pytest.raises(SystemExit):gateway.main(['--from-env'])
