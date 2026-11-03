from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.telegram.gateway_router import router
from app.telegram.gateway_routes import RouteConflict


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
async def test_route_control_auth_validation_and_error_envelopes(monkeypatch):
    app=FastAPI();app.include_router(router);app.state.db_pool=object()
    account=uuid4();principal=AuthPrincipal(uuid4(),uuid4(),uuid4(),'OWNER','test@example.test',False)
    store=SimpleNamespace(read=AsyncMock(return_value=None),change=AsyncMock())
    monkeypatch.setattr('app.telegram.gateway_router.GatewayRoutes',lambda pool:store)
    url=f'/api/v1/telegram/accounts/{account}/gateway'
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get(url)).status_code==401
        app.dependency_overrides[get_current_principal]=lambda:principal
        for body in [{'enabled':'true','expected_generation':0}, {'enabled':True,'expected_generation':True},
                     {'enabled':True,'expected_generation':-1}, {'enabled':True,'expected_generation':0,'secret':'private'}]:
            response=await client.post(url,json=body)
            assert response.status_code==422 and 'private' not in response.text
        store.change.assert_not_awaited()
        assert (await client.get(url)).status_code==404
        for error,code in [(PermissionError('private'),403),(LookupError('private'),404),
                           (RouteConflict('Route version changed'),409),(OSError('private password'),503)]:
            store.change.side_effect=error
            response=await client.post(url,json={'enabled':True,'expected_generation':0})
            assert response.status_code==code and response.json()['success'] is False
            assert 'private' not in response.text
        store.change.side_effect=None
        store.change.return_value={'account_id':account,'enabled':True,'generation':1,'last_reason':None}
        response=await client.post(url,json={'enabled':True,'expected_generation':0})
        assert response.status_code==200 and response.json()['data']['generation']==1
        store.change.assert_awaited_with(principal,account,True,0)
