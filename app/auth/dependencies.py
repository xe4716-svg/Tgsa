from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, HTTPException, Request, status

from app.auth.security import decode_access_token


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: UUID
    tenant_id: UUID
    family_id: UUID
    role: str
    email: str
    platform_admin: bool


async def get_current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthPrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    try:
        claims = decode_access_token(token)
        user_id = UUID(claims["sub"])
        tenant_id = UUID(claims["tid"])
        family_id = UUID(claims["sid"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT u.email, u.status, u.platform_admin, tm.role, t.status AS tenant_status,
                       EXISTS (
                           SELECT 1
                           FROM auth_refresh_tokens rt
                           WHERE rt.tenant_id = $1
                             AND rt.user_id = $2
                             AND rt.family_id = $3
                             AND rt.revoked_at IS NULL
                             AND rt.expires_at > NOW()
                       ) AS session_active
                FROM users u
                JOIN tenant_memberships tm
                  ON tm.user_id = u.id AND tm.tenant_id = $1
                JOIN tenants t ON t.id = tm.tenant_id
                WHERE u.id = $2
                """,
                tenant_id,
                user_id,
                family_id,
            )
            if (
                not row
                or row["status"] != "ACTIVE"
                or row["tenant_status"] != "ACTIVE"
                or not row["session_active"]
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session is no longer active",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return AuthPrincipal(
                user_id=user_id,
                tenant_id=tenant_id,
                family_id=family_id,
                role=row["role"],
                email=row["email"],
                platform_admin=row["platform_admin"],
            )
