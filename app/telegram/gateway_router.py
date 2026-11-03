"""Authenticated desired-route control, without session/proxy secret responses."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.telegram.gateway_routes import GatewayRoutes, RouteConflict
from app.telegram.router import ResourceRoute

router = APIRouter(tags=['gateway routes'], route_class=ResourceRoute)


class RouteChange(BaseModel):
    model_config = {'extra':'forbid'}
    enabled: bool = Field(strict=True)
    expected_generation: int = Field(strict=True, ge=0, lt=9223372036854775807)


@router.get('/api/v1/telegram/accounts/{account_id}/gateway')
async def read_route(account_id: UUID, request: Request,
                     principal: AuthPrincipal = Depends(get_current_principal)):
    data = await GatewayRoutes(request.app.state.db_pool).read(principal.tenant_id, account_id)
    if data is None:
        raise HTTPException(404, 'Account not found')
    return {'success':True, 'data':data, 'request_id':request.state.resource_request_id}


@router.post('/api/v1/telegram/accounts/{account_id}/gateway')
async def change_route(account_id: UUID, body: RouteChange, request: Request,
                       principal: AuthPrincipal = Depends(get_current_principal)):
    try:
        data = await GatewayRoutes(request.app.state.db_pool).change(
            principal, account_id, body.enabled, body.expected_generation)
    except PermissionError:
        raise HTTPException(403, 'Current session cannot change routes') from None
    except LookupError:
        raise HTTPException(404, 'Account not found') from None
    except RouteConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {'success':True, 'data':data, 'request_id':request.state.resource_request_id}
