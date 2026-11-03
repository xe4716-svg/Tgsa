import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncpg
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.auth.dependencies import AuthPrincipal,get_current_principal
from app.integration.identity import BusinessIdentity,BridgeDenied
import app.integration.relay_control as control
from scripts.configure_relay_control import grant_sql

@pytest.fixture
def setup(monkeypatch):
    principal=AuthPrincipal(uuid4(),uuid4(),uuid4(),'ADMIN','test@example.test',False)
    identity=BusinessIdentity(uuid4(),uuid4(),'relay.manage')
    identifier=uuid4()
    body=control.StateChange(status='paused',expected_generation=1)
    key='request-key-123'
    import hashlib
    fingerprint=hashlib.sha256(json.dumps({'id':str(identifier),'status':'paused','generation':1},sort_keys=True).encode()).hexdigest()
    record={'id':uuid4(),'request_hash':fingerprint,'expired':False,'response_status':None,'response_body':None}
    old={'id':identifier,'status':'open','generation':1}
    row={'id':identifier,'status':'paused','generation':2}
    @asynccontextmanager
    async def transaction():
        yield
    conn=SimpleNamespace(transaction=transaction,execute=AsyncMock(),
        fetchval=AsyncMock(return_value=record['id']),fetchrow=AsyncMock(side_effect=[record,old,row]))
    resolve=AsyncMock(return_value=identity)
    monkeypatch.setattr(control,'resolve_identity',resolve)
    return SimpleNamespace(conn=conn,principal=principal,identity=identity,identifier=identifier,
                           body=body,key=key,record=record,resolve=resolve)

def call(s):
    return asyncio.run(control.change_state(s.conn,s.principal,s.identifier,s.body,s.key,'server-request'))

def test_success_audits_once_and_restores_context(setup):
    s=setup
    assert call(s)=={'id':str(s.identifier),'status':'paused','generation':2}
    assert s.resolve.await_count==3
    s.resolve.assert_awaited_with(s.conn,s.principal,'relay.manage')
    audit=[c for c in s.conn.execute.call_args_list if 'INSERT INTO tgmate.audit_logs' in c.args[0]]
    assert len(audit)==1 and audit[0].args[1:4]==(s.identity.tenant_id,s.identity.user_id,s.identifier)
    assert json.loads(audit[0].args[-1])['before_generation']==1
    s.conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(s.principal.tenant_id))

def test_replay_does_not_update_or_audit(setup):
    s=setup
    value={'id':str(s.identifier),'status':'paused','generation':2}
    s.record.update(response_status=200,response_body=json.dumps(value))
    s.conn.fetchval.return_value=None
    assert call(s)==value
    assert s.conn.fetchrow.await_count==1
    assert not any('audit_logs' in c.args[0] or 'UPDATE ' in c.args[0] for c in s.conn.execute.call_args_list)

@pytest.mark.parametrize('kind',['conflict','expired','pending','version','missing'])
def test_rejection_restores_scope_without_audit(setup,kind):
    s=setup
    if kind=='conflict':s.record['request_hash']='different'
    if kind=='expired':s.record['expired']=True
    if kind=='pending':s.conn.fetchval.return_value=None
    if kind=='version':s.conn.fetchrow.side_effect=[s.record,{'generation':2}]
    if kind=='missing':s.conn.fetchrow.side_effect=[s.record,None]
    with pytest.raises(HTTPException) as error:call(s)
    assert error.value.status_code==(404 if kind=='missing' else 409)
    assert not any('audit_logs' in c.args[0] for c in s.conn.execute.call_args_list)
    s.conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(s.principal.tenant_id))

def test_audit_failure_restores_scope_and_aborts(setup):
    s=setup
    async def execute(sql,*args):
        if 'audit_logs' in sql:raise asyncpg.InsufficientPrivilegeError('private')
    s.conn.execute.side_effect=execute
    with pytest.raises(asyncpg.InsufficientPrivilegeError):call(s)
    s.conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(s.principal.tenant_id))

def test_fresh_permission_failure_never_creates_idempotency(setup):
    s=setup
    s.resolve.side_effect=BridgeDenied()
    with pytest.raises(BridgeDenied):call(s)
    s.conn.fetchval.assert_not_awaited()

def test_revocation_after_row_wait_stops_mutation(setup):
    s=setup
    s.resolve.side_effect=[s.identity,s.identity,BridgeDenied()]
    with pytest.raises(BridgeDenied):call(s)
    assert s.conn.fetchrow.await_count==2
    assert not any('audit_logs' in c.args[0] for c in s.conn.execute.call_args_list)

@pytest.mark.parametrize('body,key',[
    ({'status':'invalid','expected_generation':1},'valid-key'),
    ({'status':'paused','expected_generation':True},'valid-key'),
    ({'status':'paused','expected_generation':0},'valid-key'),
    ({'status':'paused','expected_generation':1,'tenant_id':'client-owned'},'valid-key'),
    ({'status':'paused','expected_generation':1},'bad key'),
    ({'status':'paused','expected_generation':1},None),
])
def test_http_rejects_invalid_command_before_writes(setup,body,key):
    s=setup
    @asynccontextmanager
    async def acquire():yield s.conn
    app=FastAPI();app.include_router(control.router)
    app.state.db_pool=SimpleNamespace(acquire=acquire)
    app.dependency_overrides[get_current_principal]=lambda:s.principal
    headers={'Idempotency-Key':key} if key else {}
    with TestClient(app) as client:
        response=client.post('/api/v1/relay/conversations/'+str(s.identifier)+'/state',json=body,headers=headers)
    assert response.status_code==422 and response.json()['success'] is False
    s.conn.fetchval.assert_not_awaited()

@pytest.mark.parametrize('role',["x';DROP TABLE users;--",'role with spaces','-option'])
def test_grant_rejects_unsafe_identifiers(role):
    with pytest.raises(ValueError):grant_sql(role)
