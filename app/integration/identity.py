"""Read-only identity resolution; mappings are provisioned offline, never by email."""
from dataclasses import dataclass
from uuid import UUID
import asyncpg
from app.auth.dependencies import AuthPrincipal

class BridgeDenied(Exception):
    pass

class BridgeUnavailable(Exception):
    pass

@dataclass(frozen=True)
class BusinessIdentity:
    tenant_id: UUID
    user_id: UUID
    permission: str

async def resolve_identity(conn, principal: AuthPrincipal, permission: str):
    if not isinstance(principal, AuthPrincipal) or not permission or permission == 'tenant.admin':
        raise BridgeDenied('Business permission denied')
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(principal.tenant_id))
        root = await conn.fetchrow('''SELECT m.role FROM public.tenant_memberships m
          JOIN public.users u ON u.id=m.user_id JOIN public.tenants t ON t.id=m.tenant_id
          WHERE m.tenant_id=$1 AND m.user_id=$2 AND u.status='ACTIVE' AND t.status='ACTIVE'
          AND EXISTS(SELECT 1 FROM public.auth_refresh_tokens s WHERE s.tenant_id=m.tenant_id
            AND s.user_id=m.user_id AND s.family_id=$3 AND s.revoked_at IS NULL AND s.expires_at>now())''',
          principal.tenant_id, principal.user_id, principal.family_id)
        if not root or (root['role']=='VIEWER' and not permission.endswith('.read')):
            raise BridgeDenied('Business permission denied')
        mapping = await conn.fetchrow('''SELECT m.business_tenant_id,u.business_user_id
          FROM public.integration_member_map m JOIN public.integration_user_map u ON u.user_id=m.user_id
          WHERE m.tenant_id=$1 AND m.user_id=$2''', principal.tenant_id, principal.user_id)
        if not mapping:
            raise BridgeDenied('Business identity not linked')
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(mapping['business_tenant_id']))
                allowed = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM tgmate.tenant_members m
                  JOIN tgmate.users u ON u.id=m.user_id JOIN tgmate.tenants t ON t.id=m.tenant_id
                  JOIN tgmate.tenant_member_roles mr ON mr.tenant_id=m.tenant_id AND mr.user_id=m.user_id
                  JOIN tgmate.role_permissions rp ON rp.tenant_id=mr.tenant_id AND rp.role_id=mr.role_id
                  WHERE m.tenant_id=$1 AND m.user_id=$2 AND m.status='active'
                    AND u.status='active' AND t.status='active' AND rp.permission_code=$3)''',
                  mapping['business_tenant_id'],mapping['business_user_id'],permission)
        except asyncpg.PostgresError:
            raise BridgeUnavailable('Business identity service unavailable') from None
        finally:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(principal.tenant_id))
        if not allowed:
            raise BridgeDenied('Business permission denied')
        return BusinessIdentity(mapping['business_tenant_id'],mapping['business_user_id'],permission)
