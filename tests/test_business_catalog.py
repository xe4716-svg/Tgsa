from contextlib import asynccontextmanager
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth.dependencies import AuthPrincipal,get_current_principal
from app.integration.identity import BridgeDenied
import app.integration.catalog as module
from scripts.configure_catalog_reads import grant_sql

CASES=[
('/api/v1/broadcast/bot-tasks','bot_broadcast.read','bot_broadcast_tasks',module.TASK_FIELDS+('robot_instance_id','telegram_bot_id')),
('/api/v1/broadcast/account-tasks','account_broadcast.read','account_broadcast_tasks',module.TASK_FIELDS+('execution_mode','target_scope','failure_policy')),
('/api/v1/moderation/groups','moderation.read','moderation_group_configs',module.GROUP_FIELDS),
]
@pytest.fixture
def setup(monkeypatch):
    principal=AuthPrincipal(uuid4(),uuid4(),uuid4(),'VIEWER','test@example.test',False)
    identity=SimpleNamespace(tenant_id=uuid4(),user_id=uuid4())
    conn=SimpleNamespace(execute=AsyncMock(),fetch=AsyncMock(return_value=[]))
    @asynccontextmanager
    async def context():yield conn
    conn.transaction=context
    app=FastAPI();app.include_router(module.router)
    app.state.db_pool=SimpleNamespace(acquire=context)
    app.dependency_overrides[get_current_principal]=lambda:principal
    resolve=AsyncMock(return_value=identity)
    monkeypatch.setattr(module,'resolve_identity',resolve)
    return app,conn,principal,identity,resolve

@pytest.mark.parametrize('path,permission,table,fields',CASES)
def test_separate_inventory_and_safe_fields(setup,path,permission,table,fields):
    app,conn,principal,identity,resolve=setup
    row=dict.fromkeys(fields,None)
    row.update(id=uuid4(),created_at=datetime.now(timezone.utc),payload='SECRET',welcome_payload='SECRET',token='SECRET')
    conn.fetch.return_value=[row,row]
    with TestClient(app) as client:
        first=client.get(path,params={'limit':1,'tenant_id':str(uuid4()),'sender_type':'wrong'})
        assert first.status_code==200
        data=first.json()['data']
        assert set(data['items'][0])==set(fields) and 'SECRET' not in first.text
        assert client.get(path,params={'cursor':data['next_cursor']}).status_code==200
    resolve.assert_awaited_with(conn,principal,permission)
    query,tenant,*_=conn.fetch.call_args.args
    assert table in query and tenant==identity.tenant_id
    assert 'payload' not in query and 'WHERE t.tenant_id=$1' in query
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(principal.tenant_id))

@pytest.mark.parametrize('path,permission,table,fields',CASES)
def test_permission_is_required_before_query(setup,path,permission,table,fields):
    app,conn,_,_,resolve=setup
    resolve.side_effect=BridgeDenied('private')
    with TestClient(app) as client:
        response=client.get(path)
    assert response.status_code==403 and 'private' not in response.text
    conn.fetch.assert_not_awaited()

@pytest.mark.parametrize('path,permission,table,fields',CASES)
def test_database_failure_restores_scope(setup,path,permission,table,fields):
    app,conn,principal,_,_=setup
    conn.fetch.side_effect=asyncpg.InsufficientPrivilegeError('private SQL')
    with TestClient(app) as client:response=client.get(path)
    assert response.status_code==503 and 'private SQL' not in response.text
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(principal.tenant_id))

@pytest.mark.parametrize('path,permission,table,fields',CASES)
@pytest.mark.parametrize('params,status',[({'limit':0},422),({'limit':101},422),({'cursor':'bad'},400)])
def test_invalid_pagination_does_not_query(setup,path,permission,table,fields,params,status):
    app,conn,_,_,resolve=setup
    with TestClient(app) as client:assert client.get(path,params=params).status_code==status
    resolve.assert_not_awaited();conn.fetch.assert_not_awaited()

@pytest.mark.parametrize('path,permission,table,fields',CASES)
def test_login_required(setup,path,permission,table,fields):
    app,conn,*_=setup
    app.dependency_overrides.clear()
    with TestClient(app) as client:assert client.get(path).status_code==401
    conn.fetch.assert_not_awaited()

@pytest.mark.parametrize('role',["'; DROP TABLE users;--",'bad role','-option'])
def test_grant_identifier_validation(role):
    with pytest.raises(ValueError):grant_sql(role)
