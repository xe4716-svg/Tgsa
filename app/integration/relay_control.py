"""Version-checked, idempotent customer-relay state controls."""
import hashlib
import json
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.integration.identity import resolve_identity, BridgeDenied, BridgeUnavailable
from app.telegram.router import ResourceRoute

router = APIRouter(tags=['customer relay control'], route_class=ResourceRoute)
SCOPE = 'relay.conversation.state.v1'

class StateChange(BaseModel):
    model_config={'extra':'forbid'}
    status: Literal['open','paused','blocked','closed']
    expected_generation: int = Field(ge=1,le=9223372036854775807,strict=True)

async def confirm_after_wait(conn,principal,identity):
    current=await resolve_identity(conn,principal,'relay.manage')
    if (current.tenant_id,current.user_id)!=(identity.tenant_id,identity.user_id):
        raise BridgeDenied('Business identity changed')
    await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(identity.tenant_id))

async def change_state(conn, principal, identifier, body, key, request_id):
    fingerprint = hashlib.sha256(json.dumps({
        'id':str(identifier),'status':body.status,'generation':body.expected_generation
    },sort_keys=True).encode()).hexdigest()
    async with conn.transaction():
        identity = await resolve_identity(conn,principal,'relay.manage')
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(identity.tenant_id))
                created = await conn.fetchval("""INSERT INTO tgmate.idempotency_records
                    (tenant_id,scope,idempotency_key,request_hash,expires_at)
                    VALUES($1,$2,$3,$4,now()+interval '24 hours')
                    ON CONFLICT(tenant_id,scope,idempotency_key) DO NOTHING RETURNING id""",
                    identity.tenant_id,SCOPE,key,fingerprint)
                record = await conn.fetchrow("""SELECT id,request_hash,response_status,response_body,
                    expires_at<=now() AS expired FROM tgmate.idempotency_records
                    WHERE tenant_id=$1 AND scope=$2 AND idempotency_key=$3 FOR UPDATE""",
                    identity.tenant_id,SCOPE,key)
                await confirm_after_wait(conn,principal,identity)
                if not record or record['request_hash']!=fingerprint:
                    raise HTTPException(409,'Idempotency key conflicts with another request')
                if record['expired']:
                    raise HTTPException(409,'Idempotency key expired; use a new key')
                if record['response_body'] is not None and record['response_status']==200:
                    value=record['response_body']
                    return json.loads(value) if isinstance(value,str) else value
                if not created:
                    raise HTTPException(409,'Previous result requires review')
                old=await conn.fetchrow("""SELECT id,status,generation FROM tgmate.relay_conversations
                    WHERE tenant_id=$1 AND id=$2 FOR UPDATE""",identity.tenant_id,identifier)
                await confirm_after_wait(conn,principal,identity)
                if not old:
                    raise HTTPException(404,'Conversation not found')
                if old['generation']!=body.expected_generation:
                    raise HTTPException(409,'Conversation version changed; reload before retrying')
                row=await conn.fetchrow("""UPDATE tgmate.relay_conversations SET status=$3
                    WHERE tenant_id=$1 AND id=$2 AND generation=$4 RETURNING id,status,generation""",
                    identity.tenant_id,identifier,body.status,body.expected_generation)
                if not row:
                    raise HTTPException(409,'Conversation version changed')
                result={'id':str(row['id']),'status':row['status'],'generation':row['generation']}
                await conn.execute("""INSERT INTO tgmate.audit_logs
                    (tenant_id,actor_user_id,action,resource_type,resource_id,request_id,metadata)
                    VALUES($1,$2,'relay.conversation.state','relay_conversation',$3,$4,$5::jsonb)""",
                    identity.tenant_id,identity.user_id,identifier,request_id,
                    json.dumps({'before':old['status'],'after':body.status,
                                'before_generation':old['generation'],'generation':row['generation']}))
                await conn.execute("""UPDATE tgmate.idempotency_records SET response_status=200,response_body=$3::jsonb
                    WHERE tenant_id=$1 AND id=$2""",identity.tenant_id,record['id'],json.dumps(result))
                return result
        finally:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(principal.tenant_id))

@router.post('/api/v1/relay/conversations/{identifier}/state')
async def conversation_state(identifier:UUID,body:StateChange,request:Request,
    idempotency_key:str=Header(pattern=r'^[A-Za-z0-9._:-]{8,128}$'),
    principal:AuthPrincipal=Depends(get_current_principal)):
    async with request.app.state.db_pool.acquire() as conn:
        try:
            data=await change_state(conn,principal,identifier,body,idempotency_key,request.state.resource_request_id)
        except BridgeDenied:
            raise HTTPException(403,'Customer relay management denied') from None
        except BridgeUnavailable:
            raise HTTPException(503,'Customer relay unavailable') from None
    return {'success':True,'data':data,'request_id':request.state.resource_request_id}
