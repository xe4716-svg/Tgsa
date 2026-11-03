from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth.dependencies import AuthPrincipal, get_current_principal
import app.integration.router as module
from app.integration.identity import BridgeDenied, BridgeUnavailable

@pytest.fixture
def setup(monkeypatch):
    principal=AuthPrincipal(uuid4(),uuid4(),uuid4(),'VIEWER','test@example.test',False)
    identity=SimpleNamespace(tenant_id=uuid4(),user_id=uuid4())
    conn=SimpleNamespace(execute=AsyncMock(),fetch=AsyncMock(return_value=[]))
    @asynccontextmanager
    async def context():
        yield conn
    conn.transaction=context
    app=FastAPI()
    app.include_router(module.router)
    app.state.db_pool=SimpleNamespace(acquire=context)
    app.dependency_overrides[get_current_principal]=lambda:principal
    resolve=AsyncMock(return_value=identity)
    monkeypatch.setattr(module,'resolve_identity',resolve)
    return app,conn,principal,identity,resolve

def test_metadata_and_mapped_tenant(setup):
    app,conn,p,identity,resolve=setup
    row=dict.fromkeys(module.FIELDS,None)
    row.update(id=uuid4(),created_at=datetime.now(timezone.utc),customer_user_id=123,ciphertext='secret')
    conn.fetch.return_value=[row,row]
    with TestClient(app) as client:
        response=client.get('/api/v1/relay/conversations',params={'limit':1,'tenant_id':str(uuid4())})
        assert response.status_code==200
        body=response.json()
        assert set(body['data']['items'][0])==set(module.FIELDS)
        assert 'secret' not in response.text and 'customer_user_id' not in response.text
        cursor=body['data']['next_cursor']
        assert client.get('/api/v1/relay/conversations',params={'cursor':cursor}).status_code==200
    resolve.assert_awaited_with(conn,p,'relay.read')
    sql,tenant,*_=conn.fetch.call_args.args
    assert tenant==identity.tenant_id and 'WHERE tenant_id=$1' in sql
    assert 'customer_user_id' not in sql and 'ciphertext' not in sql
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(p.tenant_id))

@pytest.mark.parametrize('error,status',[(BridgeDenied('private'),403),(BridgeUnavailable('private'),503)])
def test_bridge_rejection(setup,error,status):
    app,conn,_,_,resolve=setup
    resolve.side_effect=error
    with TestClient(app) as client:
        response=client.get('/api/v1/relay/conversations')
    assert response.status_code==status and 'private' not in response.text
    conn.fetch.assert_not_awaited()

def test_sql_failure_restores_context(setup):
    app,conn,p,_,_=setup
    conn.fetch.side_effect=asyncpg.UndefinedTableError('private SQL')
    with TestClient(app) as client:
        response=client.get('/api/v1/relay/conversations')
    assert response.status_code==503 and 'private SQL' not in response.text
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(p.tenant_id))

@pytest.mark.parametrize('params,status',[({'limit':0},422),({'limit':101},422),({'cursor':'bad'},400)])
def test_invalid_input(setup,params,status):
    app,conn,_,_,resolve=setup
    with TestClient(app) as client:
        assert client.get('/api/v1/relay/conversations',params=params).status_code==status
    resolve.assert_not_awaited()
    conn.fetch.assert_not_awaited()

def test_auth_required(setup):
    app,conn,*_=setup
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        assert client.get('/api/v1/relay/conversations').status_code==401
    conn.fetch.assert_not_awaited()
