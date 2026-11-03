import asyncio
from types import SimpleNamespace
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import psycopg
import pytest

from app import bot_main
from app.business import db
from app.business.errors import DeferredIngress
from app.business.relay import Rejected


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
async def test_bot_ingress_authenticates_before_parsing_and_preserves_context(monkeypatch):
    app=bot_main.app;config={'allowed_tenants':frozenset()};calls=[]
    app.state.business_config=config;app.state.ingress_slots=asyncio.Semaphore(1)
    app.state.worker_task=SimpleNamespace(done=lambda:False)
    def ingress(bot,secret):
        assert db.settings() is config
        calls.append('authenticate')
        return {'tenant_id':'assigned'} if secret=='s'*100 else None
    def dispatch(tenant,bot,update):
        assert db.settings() is config and tenant=='assigned'
        calls.append('dispatch');return {'status':'duplicate'}
    monkeypatch.setattr(bot_main.bot_webhooks,'ingress',ingress)
    monkeypatch.setattr(bot_main.bot_gateway,'dispatch',dispatch)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        url='/webhooks/bots/'+str(uuid4())
        assert (await client.post(url,json={})).status_code==403
        assert calls==[]
        assert (await client.post(url,content=b'invalid',headers={'X-Telegram-Bot-Api-Secret-Token':'x'*100})).status_code==403
        assert calls==['authenticate']
        headers={'X-Telegram-Bot-Api-Secret-Token':'s'*100}
        assert (await client.post(url,content=b'invalid',headers=headers)).status_code==422
        assert (await client.post(url,content=b'x'*(1024*1024+1),headers=headers)).status_code==413
        response=await client.post(url,json={'update_id':1},headers=headers)
        assert response.status_code==200 and response.json()=={'status':'duplicate'}
        assert calls.count('dispatch')==1
        assert (await client.get('/health/ready')).status_code==200
        app.state.worker_task=SimpleNamespace(done=lambda:True)
        assert (await client.get('/health/ready')).status_code==503
        assert (await client.post(url,json={},headers=headers)).status_code==503
    assert db._runtime_settings.get() is None


@pytest.mark.anyio
@pytest.mark.parametrize('error,status',[(PermissionError('private'),403),(psycopg.OperationalError('private'),503),
                                        (DeferredIngress('private'),503),(Rejected('private'),422)])
async def test_ingress_failure_is_sanitized(monkeypatch,error,status):
    app=bot_main.app
    app.state.business_config={};app.state.ingress_slots=asyncio.Semaphore(1)
    app.state.worker_task=SimpleNamespace(done=lambda:False)
    def fail(*args):raise error
    monkeypatch.setattr(bot_main.bot_webhooks,'ingress',fail)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/webhooks/bots/'+str(uuid4()),json={},headers={'X-Telegram-Bot-Api-Secret-Token':'s'*100})
        assert response.status_code==status and 'private' not in response.text
