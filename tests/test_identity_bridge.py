import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncpg
import pytest
from app.auth.dependencies import AuthPrincipal
from app.integration.identity import resolve_identity, BridgeDenied, BridgeUnavailable

@asynccontextmanager
async def transaction():
    yield

def fixture(role='ADMIN', mapped=True, allowed=True):
    principal=AuthPrincipal(uuid4(),uuid4(),uuid4(),'ADMIN','test@example.test',False)
    mapping={'business_tenant_id':uuid4(),'business_user_id':uuid4()}
    class Connection:
        pass
    conn=Connection()
    conn.transaction=transaction
    conn.execute=AsyncMock()
    conn.fetchrow=AsyncMock(side_effect=[{'role':role},mapping if mapped else None])
    conn.fetchval=AsyncMock(return_value=allowed)
    return conn,principal,mapping

def test_resolves_and_restores_context():
    conn,p,m=fixture()
    result=asyncio.run(resolve_identity(conn,p,'relay.read'))
    assert result.tenant_id==m['business_tenant_id']
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(p.tenant_id))
    assert conn.fetchval.call_args.args[1:]==(m['business_tenant_id'],m['business_user_id'],'relay.read')

@pytest.mark.parametrize('options,permission', [({'role':'VIEWER'},'relay.manage'),({'mapped':False},'relay.read'),({'allowed':False},'relay.read'),({},'tenant.admin')])
def test_denied(options,permission):
    conn,p,_=fixture(**options)
    with pytest.raises(BridgeDenied):
        asyncio.run(resolve_identity(conn,p,permission))

def test_root_revocation():
    conn,p,_=fixture()
    conn.fetchrow.side_effect=[None]
    with pytest.raises(BridgeDenied):
        asyncio.run(resolve_identity(conn,p,'relay.read'))
    conn.fetchval.assert_not_awaited()

def test_missing_schema_is_safe_and_restores_context():
    conn,p,_=fixture()
    conn.fetchval.side_effect=asyncpg.UndefinedTableError('private SQL')
    with pytest.raises(BridgeUnavailable,match='Business identity service unavailable'):
        asyncio.run(resolve_identity(conn,p,'relay.read'))
    conn.execute.assert_awaited_with("SELECT set_config('app.tenant_id',$1,true)",str(p.tenant_id))
