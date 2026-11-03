"""Separate, permission-scoped business inventories."""
import base64
import json
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from app.auth.dependencies import AuthPrincipal,get_current_principal
from app.integration.identity import resolve_identity,BridgeDenied,BridgeUnavailable
from app.telegram.router import ResourceRoute,decode_cursor

router=APIRouter(tags=['business inventories'],route_class=ResourceRoute)
TASK_FIELDS=('id','status','task_type','scheduled_at','started_at','finished_at','created_at','updated_at')
GROUP_FIELDS=('id','destination_id','enabled','cleanup_mode','violation_threshold','threshold_action','verification_enabled','created_at','updated_at')

async def inventory(request,principal,permission,kind,limit,cursor):
    timestamp,identifier=decode_cursor(cursor)
    if kind=='moderation':
        fields=GROUP_FIELDS
        selection=', '.join('t.'+field for field in fields)
        source='tgmate.moderation_group_configs t'
        condition=''
    else:
        fields=TASK_FIELDS+(('robot_instance_id','telegram_bot_id') if kind=='bot' else ('execution_mode','target_scope','failure_policy'))
        selection=', '.join('t.'+field for field in TASK_FIELDS)
        selection+=', t.robot_instance_id, b.telegram_bot_id' if kind=='bot' else ', b.execution_mode, b.target_scope, b.failure_policy'
        table='bot_broadcast_tasks' if kind=='bot' else 'account_broadcast_tasks'
        source=f'tgmate.task_registry t JOIN tgmate.{table} b ON b.tenant_id=t.tenant_id AND b.task_id=t.id'
        module='bot_broadcast' if kind=='bot' else 'account_broadcast'
        condition=f" AND t.business_module='{module}'"
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            try:
                identity=await resolve_identity(conn,principal,permission)
            except BridgeDenied:
                raise HTTPException(403,'Business inventory access denied') from None
            except BridgeUnavailable:
                raise HTTPException(503,'Business inventory unavailable') from None
            try:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(identity.tenant_id))
                    rows=await conn.fetch(f"""SELECT {selection} FROM {source}
                        WHERE t.tenant_id=$1 {condition}
                        AND ($2::timestamptz IS NULL OR (t.created_at,t.id)<($2,$3::uuid))
                        ORDER BY t.created_at DESC,t.id DESC LIMIT $4""",
                        identity.tenant_id,timestamp,identifier,limit+1)
            finally:
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(principal.tenant_id))
    items=[{field:row[field] for field in fields} for row in rows[:limit]]
    next_cursor=None
    if len(rows)>limit:
        last=items[-1]
        next_cursor=base64.urlsafe_b64encode(json.dumps({'created_at':last['created_at'].isoformat(),'id':str(last['id'])}).encode()).decode()
    return {'success':True,'data':{'items':items,'next_cursor':next_cursor},'request_id':request.state.resource_request_id}

@router.get('/api/v1/broadcast/bot-tasks')
async def bot_tasks(request:Request,limit:int=Query(20,ge=1,le=100),cursor:str|None=None,
                    principal:AuthPrincipal=Depends(get_current_principal)):
    return await inventory(request,principal,'bot_broadcast.read','bot',limit,cursor)

@router.get('/api/v1/broadcast/account-tasks')
async def account_tasks(request:Request,limit:int=Query(20,ge=1,le=100),cursor:str|None=None,
                        principal:AuthPrincipal=Depends(get_current_principal)):
    return await inventory(request,principal,'account_broadcast.read','account',limit,cursor)

@router.get('/api/v1/moderation/groups')
async def moderation_groups(request:Request,limit:int=Query(20,ge=1,le=100),cursor:str|None=None,
                            principal:AuthPrincipal=Depends(get_current_principal)):
    return await inventory(request,principal,'moderation.read','moderation',limit,cursor)
