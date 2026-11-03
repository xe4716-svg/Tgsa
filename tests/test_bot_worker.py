import asyncio
import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.business import db,worker
from app.workers import bot


@pytest.fixture
def anyio_backend():return 'asyncio'


def test_business_imports_share_original_source_without_shadowing_root_auth():
    import app.auth.dependencies
    for name in ('worker','broadcast_worker','private_worker','moderation_worker','verification_worker','bot_gateway','bot_webhooks'):
        module=importlib.import_module('app.business.'+name)
        assert 'workstreams/tgmate-v2/app' in module.__file__
    assert importlib.import_module('app.auth.dependencies') is app.auth.dependencies
    assert worker.RetryLater(200000).seconds==200000


@pytest.mark.anyio
async def test_scheduler_rotates_tenants_and_propagates_private_context_to_threads():
    tenants=[uuid4(),uuid4()];identifier=uuid4();stop=asyncio.Event();calls=[]
    config={'private-marker':'never-log'}
    def job(tenant,worker_id):
        assert db.settings() is config and worker_id==identifier
        calls.append(tenant)
        if len(calls)==4:stop.set()
        return {'status':'idle'}
    await bot.serve(tenants,identifier,stop,config=config,jobs=[('test',job)],poll_interval=.001)
    assert calls==[tenants[0],tenants[1],tenants[1],tenants[0]]
    assert db._runtime_settings.get() is None


@pytest.mark.anyio
async def test_suspended_tenant_does_not_block_another_tenant():
    tenants=[uuid4(),uuid4()];stop=asyncio.Event();calls=[]
    def job(tenant,identifier):
        calls.append(tenant)
        if tenant==tenants[0]:raise PermissionError('suspended')
        stop.set()
    await bot.serve(tenants,uuid4(),stop,config={},jobs=[('test',job)])
    assert calls==tenants


@pytest.mark.anyio
async def test_stop_waits_for_current_operation_and_does_not_start_next():
    stop=asyncio.Event();calls=[]
    def job(*args):calls.append(True);stop.set()
    await bot.serve([uuid4()],uuid4(),stop,config={},jobs=[('first',job),('second',job)])
    assert calls==[True]


def test_bot_worker_requires_explicit_live_and_private_configuration(monkeypatch):
    with pytest.raises(SystemExit):bot.main(['--tenant',str(uuid4()),'--worker',str(uuid4())])
    monkeypatch.delenv('BOT_WORKER_DATABASE_URL',raising=False)
    monkeypatch.delenv('BUSINESS_VAULT_KEY',raising=False)
    with pytest.raises(ValueError):bot.runtime_settings(uuid4())


def test_bot_job_table_uses_only_bot_broadcast(monkeypatch):
    calls=[]
    monkeypatch.setattr(bot.broadcast_worker,'run_once',lambda *args,**kwargs:calls.append(kwargs))
    jobs=dict(bot.build_jobs(object()))
    jobs['bot_broadcast'](uuid4(),uuid4())
    assert calls==[{'business_module':'bot_broadcast'}]


@pytest.mark.anyio
async def test_worker_start_failure_is_sanitized(monkeypatch,capsys):
    def fail(*args):raise ValueError('private password')
    monkeypatch.setattr(bot,'runtime_settings',fail)
    assert await bot.run([uuid4()],uuid4())==2
    output=capsys.readouterr().out
    assert 'BOT_WORKER_FAILED' in output and 'private' not in output
