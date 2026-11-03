"""Tenant-scoped, read-only resource inventory. Secrets never enter responses."""
import asyncpg
import base64
import json
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.auth.dependencies import AuthPrincipal, get_current_principal

class ResourceRoute(APIRoute):
    """Apply the v1 envelope only to this router, including dependency failures."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            request.state.resource_request_id = str(uuid4())
            try:
                return await handler(request)
            except (asyncpg.PostgresError, OSError, TimeoutError):
                status, code, message, headers = 503, 'DATABASE_UNAVAILABLE', 'Database temporarily unavailable', None
            except RequestValidationError:
                status, code, message, headers = 422, 'VALIDATION_ERROR', 'Invalid request parameters', None
            except StarletteHTTPException as exc:
                status = exc.status_code
                code = {400: 'INVALID_REQUEST', 401: 'UNAUTHENTICATED',
                        403: 'FORBIDDEN', 404: 'NOT_FOUND'}.get(status, 'HTTP_ERROR')
                message = exc.detail if isinstance(exc.detail, str) else 'Request failed'
                headers = exc.headers
            return JSONResponse(status_code=status, headers=headers, content={
                'success': False,
                'error': {'code': code, 'message': message, 'retryable': status in (429, 502, 503, 504)},
                'request_id': request.state.resource_request_id,
            })
        return wrapped


router = APIRouter(tags=['telegram resources'], route_class=ResourceRoute)
ACCOUNT_FIELDS = ('id', 'telegram_user_id', 'phone_masked', 'username', 'display_name',
                  'status', 'security_state', 'connection_mode', 'created_at', 'updated_at')
PROXY_FIELDS = ('id', 'proxy_type', 'host', 'port', 'claimed_sharing_mode',
                'verified_sharing_mode', 'verified_egress_ip', 'verification_status', 'status', 'created_at')


def decode_cursor(cursor):
    if cursor is None:
        return None, None
    try:
        if len(cursor) > 512:
            raise ValueError()
        data = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
        timestamp = datetime.fromisoformat(data['created_at'])
        identifier = UUID(data['id'])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError()
        return timestamp, identifier
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(status_code=400, detail='Invalid cursor') from None


async def inventory(request, principal, table, fields, limit, cursor):
    timestamp, identifier = decode_cursor(cursor)
    # Table and columns come only from module constants, never request values.
    sql = f'''SELECT {', '.join(fields)} FROM {table}
              WHERE tenant_id = $1
                AND ($2::timestamptz IS NULL OR (created_at, id) < ($2, $3::uuid))
              ORDER BY created_at DESC, id DESC LIMIT $4'''
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            rows = await conn.fetch(sql, principal.tenant_id, timestamp, identifier, limit + 1)
    items = [{field: row[field] for field in fields} for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            'created_at': last['created_at'].isoformat(), 'id': str(last['id'])
        }).encode()).decode()
    return {'success': True, 'data': {'items': items, 'next_cursor': next_cursor},
            'request_id': request.state.resource_request_id}


@router.get('/api/v1/telegram/accounts')
async def list_accounts(request: Request, limit: int = Query(20, ge=1, le=100),
                        cursor: str | None = None,
                        principal: AuthPrincipal = Depends(get_current_principal)):
    return await inventory(request, principal, 'telegram_accounts', ACCOUNT_FIELDS, limit, cursor)


@router.get('/api/v1/proxies')
async def list_proxies(request: Request, limit: int = Query(20, ge=1, le=100),
                       cursor: str | None = None,
                       principal: AuthPrincipal = Depends(get_current_principal)):
    return await inventory(request, principal, 'proxy_profiles', PROXY_FIELDS, limit, cursor)
