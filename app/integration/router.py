"""Read-only business APIs using the unified platform identity."""
import base64
import json
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.integration.identity import resolve_identity, BridgeDenied, BridgeUnavailable
from app.telegram.router import ResourceRoute, decode_cursor

router = APIRouter(tags=['customer relay'], route_class=ResourceRoute)
FIELDS = ('id', 'config_id', 'generation', 'status', 'initiated_at', 'privacy_notice_version', 'created_at')

@router.get('/api/v1/relay/conversations')
async def conversations(request: Request, limit: int = Query(20, ge=1, le=100),
                        cursor: str | None = None,
                        principal: AuthPrincipal = Depends(get_current_principal)):
    timestamp, identifier = decode_cursor(cursor)
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            try:
                identity = await resolve_identity(conn, principal, 'relay.read')
            except BridgeDenied:
                raise HTTPException(403, 'Customer relay access denied') from None
            except BridgeUnavailable:
                raise HTTPException(503, 'Customer relay unavailable') from None
            try:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(identity.tenant_id))
                    rows = await conn.fetch(f"""SELECT {', '.join(FIELDS)} FROM tgmate.relay_conversations
                        WHERE tenant_id=$1 AND ($2::timestamptz IS NULL OR (created_at,id)<($2,$3::uuid))
                        ORDER BY created_at DESC,id DESC LIMIT $4""",
                        identity.tenant_id, timestamp, identifier, limit+1)
            finally:
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(principal.tenant_id))
    items = [{field: row[field] for field in FIELDS} for row in rows[:limit]]
    next_cursor = None
    if len(rows)>limit:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            'created_at':last['created_at'].isoformat(),'id':str(last['id'])
        }).encode()).decode()
    return {'success':True,'data':{'items':items,'next_cursor':next_cursor},
            'request_id':request.state.resource_request_id}
