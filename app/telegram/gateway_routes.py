"""Durable desired routes; reuses gateway leases and never contains credentials."""
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

from app.telegram.authorization import lock_login_authority
from app.observability import current_traceparent


class RouteConflict(ValueError):
    pass


@dataclass(frozen=True)
class GatewayRoute:
    tenant_id: UUID
    account_id: UUID
    generation: int
    # Correlation metadata is consumed once by the supervisor and is not part
    # of route identity, so clearing it never restarts a healthy connection.
    traceparent: str | None = field(default=None, compare=False, repr=False)


class GatewayRoutes:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self, tenant_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
                yield conn

    async def read(self, tenant_id, account_id):
        async with self.transaction(tenant_id) as conn:
            row = await conn.fetchrow('''SELECT a.id AS account_id,
                COALESCE(r.enabled,false) AS enabled, COALESCE(r.generation,0) AS generation,
                r.last_reason, l.lease_expires_at
                FROM telegram_accounts a LEFT JOIN account_gateway_routes r
                  ON r.tenant_id=a.tenant_id AND r.account_id=a.id
                LEFT JOIN account_gateway_leases l ON l.tenant_id=a.tenant_id AND l.account_id=a.id
                WHERE a.tenant_id=$1 AND a.id=$2''', tenant_id, account_id)
            return dict(row) if row else None

    async def change(self, principal, account_id, enabled, expected_generation):
        # Persist only the active server span. Incoming baggage/tracestate and
        # all request headers remain outside the durable control-plane record.
        traceparent = current_traceparent()
        async with self.transaction(principal.tenant_id) as conn:
            # Same lock ordering as account creation. Recheck authority after waits.
            await conn.fetchrow('SELECT id FROM tenants WHERE id=$1 FOR UPDATE', principal.tenant_id)
            account = await conn.fetchrow('''SELECT status,security_state FROM telegram_accounts
                WHERE tenant_id=$1 AND id=$2 FOR UPDATE''', principal.tenant_id, account_id)
            await lock_login_authority(conn, principal.tenant_id, principal.user_id, principal.family_id)
            if not account:
                raise LookupError('Account not found')
            if enabled and (account['status'] not in ('AUTHORIZED','ACTIVE')
                            or account['security_state']=='GATEWAY_CLOSE_UNCERTAIN'):
                raise RouteConflict('Account requires recovery before routing')
            old = await conn.fetchrow('''SELECT generation FROM account_gateway_routes
                WHERE tenant_id=$1 AND account_id=$2 FOR UPDATE''', principal.tenant_id, account_id)
            if (old['generation'] if old else 0) != expected_generation:
                raise RouteConflict('Route version changed; reload before retrying')
            if not old:
                count = await conn.fetchval('SELECT count(*) FROM account_gateway_routes WHERE tenant_id=$1', principal.tenant_id)
                if count >= 1000:
                    raise RouteConflict('Route capacity reached')
            row = await conn.fetchrow('''INSERT INTO account_gateway_routes(tenant_id,account_id,enabled,traceparent)
                VALUES($1,$2,$3,$4) ON CONFLICT(account_id) DO UPDATE
                SET enabled=EXCLUDED.enabled,generation=account_gateway_routes.generation+1,
                    traceparent=EXCLUDED.traceparent,
                    last_reason=NULL,updated_at=clock_timestamp()
                RETURNING account_id,enabled,generation,last_reason''',
                principal.tenant_id, account_id, enabled, traceparent)
            await conn.execute('''INSERT INTO audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id,details)
                VALUES($1,$2,'GATEWAY_ROUTE_CHANGED','telegram_account',$3,
                jsonb_build_object('enabled',$4::boolean,'generation',$5::bigint))''',
                principal.tenant_id, principal.user_id, account_id, enabled, row['generation'])
            await lock_login_authority(conn, principal.tenant_id, principal.user_id, principal.family_id)
            return dict(row)

    async def enabled(self, tenant_id):
        async with self.transaction(tenant_id) as conn:
            rows = await conn.fetch('''SELECT r.account_id,r.generation,r.traceparent
                FROM account_gateway_routes r
                JOIN tenants t ON t.id=r.tenant_id AND t.status='ACTIVE'
                JOIN telegram_accounts a ON a.tenant_id=r.tenant_id AND a.id=r.account_id
                WHERE r.tenant_id=$1 AND r.enabled AND a.status IN ('AUTHORIZED','ACTIVE')
                ORDER BY r.account_id
                FOR UPDATE OF r''', tenant_id)
            routes = []
            for row in rows:
                # Claim the API->worker context exactly once. The row lock makes
                # this safe when multiple supervisor processes poll the tenant.
                if row['traceparent'] is not None:
                    await conn.execute('''UPDATE account_gateway_routes SET traceparent=NULL
                        WHERE tenant_id=$1 AND account_id=$2 AND generation=$3 AND traceparent=$4''',
                        tenant_id, row['account_id'], row['generation'], row['traceparent'])
                routes.append(GatewayRoute(
                    tenant_id, row['account_id'], row['generation'], row['traceparent']))
            return routes

    async def pause(self, route, reason):
        # CAS prevents an old lifecycle from overwriting a newer operator action.
        reasons = {'AUTHORIZATION_INVALID','IDENTITY_MISMATCH','CONNECTION_CLOSED','GATEWAY_FAILED',
                   'MATERIAL_CHANGED','MATERIAL_UNAVAILABLE','OWNERSHIP_LOST','FLOOD_WAIT',
                   'WAIT_PERSIST_FAILED','DISCONNECT_UNCERTAIN','UNEXPECTED_STOP'}
        if reason not in reasons:
            reason = 'GATEWAY_FAILED'
        async with self.transaction(route.tenant_id) as conn:
            changed = await conn.fetchval('''UPDATE account_gateway_routes SET enabled=false,traceparent=NULL,
                generation=generation+1,last_reason=$4,updated_at=clock_timestamp()
                WHERE tenant_id=$1 AND account_id=$2 AND generation=$3 AND enabled RETURNING true''',
                route.tenant_id, route.account_id, route.generation, reason)
            if changed:
                await conn.execute('''INSERT INTO audit_logs(tenant_id,action,resource_type,resource_id,details)
                    VALUES($1,'GATEWAY_ROUTE_PAUSED','telegram_account',$2,jsonb_build_object('reason',$3::text))''',
                    route.tenant_id, route.account_id, reason)
            return bool(changed)
