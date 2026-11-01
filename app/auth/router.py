from __future__ import annotations

import asyncio
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.auth.mailer import send_password_reset_email, send_verification_email
from app.auth.security import (
    create_access_token,
    create_scoped_secret,
    dummy_password_hash,
    hash_password,
    hash_secret,
    normalize_email,
    parse_scoped_secret,
    validate_password,
    verify_password,
)
from app.config import settings


router = APIRouter(prefix="/auth", tags=["authentication"])
EMAIL_TOKEN_TTL_MINUTES = 30


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128)
    tenant_name: str = Field(min_length=2, max_length=120)


class EmailTokenRequest(BaseModel):
    token: str = Field(min_length=40, max_length=512)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=40, max_length=512)


class SwitchTenantRequest(BaseModel):
    tenant_id: UUID


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=40, max_length=512)
    new_password: str = Field(min_length=12, max_length=128)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _rate_limit(request: Request, key: str, limit: int, window: int) -> None:
    try:
        redis = request.app.state.redis
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window)
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )
    except HTTPException:
        raise
    except Exception:
        # Auth rate limiting is fail-closed: do not process auth requests
        # when the shared limiter is unavailable.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable",
        )


def _request_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _issue_refresh_token(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    user_id: UUID,
    family_id: UUID | None = None,
) -> str:
    family = family_id or uuid4()
    token = create_scoped_secret(tenant_id)
    token_hash = hash_secret(token)
    expires_at = _utcnow() + timedelta(days=settings.refresh_token_ttl_days)
    await conn.execute(
        """
        INSERT INTO auth_refresh_tokens (
            token_hash, tenant_id, user_id, family_id, expires_at
        )
        VALUES ($1, $2, $3, $4, $5)
        """,
        token_hash,
        tenant_id,
        user_id,
        family,
        expires_at,
    )
    return token


def _session_response(
    user_id: UUID,
    tenant_id: UUID,
    family_id: UUID,
    role: str,
    refresh_token: str,
) -> dict:
    access = create_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        family_id=family_id,
        role=role,
    )
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_seconds,
        "refresh_token": refresh_token,
    }


async def _create_email_token(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    user_id: UUID,
    table: str,
) -> str:
    if table not in {"email_verification_tokens", "password_reset_tokens"}:
        raise ValueError("Invalid token table")
    token = create_scoped_secret(tenant_id)
    await conn.execute(
        f"""
        INSERT INTO {table} (token_hash, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        hash_secret(token),
        tenant_id,
        user_id,
        _utcnow() + timedelta(minutes=EMAIL_TOKEN_TTL_MINUTES),
    )
    return token


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
async def register(payload: RegisterRequest, request: Request):
    try:
        email = normalize_email(payload.email)
        validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    await _rate_limit(
        request, f"auth:register:ip:{_request_ip(request)}", 5, 3600
    )
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    tenant_id, user_id = uuid4(), uuid4()

    try:
        async with request.app.state.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                await conn.execute(
                    """
                    INSERT INTO tenants (id, name, status)
                    VALUES ($1, $2, 'ACTIVE')
                    """,
                    tenant_id,
                    payload.tenant_name.strip(),
                )
                await conn.execute(
                    """
                    INSERT INTO users (
                        id, auth_subject, email, password_hash,
                        status, default_tenant_id
                    )
                    VALUES ($1, $2, $2, $3, 'PENDING_VERIFICATION', $4)
                    """,
                    user_id,
                    email,
                    password_hash,
                    tenant_id,
                )
                await conn.execute(
                    """
                    INSERT INTO tenant_memberships (tenant_id, user_id, role)
                    VALUES ($1, $2, 'OWNER')
                    """,
                    tenant_id,
                    user_id,
                )
                free_plan = await conn.fetchrow(
                    "SELECT id FROM membership_plans WHERE code = 'FREE' AND is_active = TRUE"
                )
                if not free_plan:
                    raise HTTPException(status_code=503, detail="Membership plans are not initialized")
                await conn.execute(
                    """
                    INSERT INTO tenant_subscriptions (
                        tenant_id, plan_id, status, billing_source
                    )
                    VALUES ($1, $2, 'ACTIVE', 'MANUAL')
                    """,
                    tenant_id,
                    free_plan["id"],
                )
                verification_token = await _create_email_token(
                    conn, tenant_id, user_id, "email_verification_tokens"
                )
    except asyncpg.UniqueViolationError:
        # Avoid disclosing whether an email address is already registered.
        return {
            "status": "verification_pending",
            "message": "If this address can be registered, a verification email will be sent.",
        }

    try:
        await send_verification_email(email, verification_token)
    except Exception:
        # The user and expiring token remain persisted; resend endpoint can retry.
        raise HTTPException(
            status_code=503,
            detail="Account created, but verification email could not be sent. Please retry later.",
        )
    return {
        "status": "verification_pending",
        "message": "Check your email to verify your account.",
    }


@router.post("/verify-email")
async def verify_email(payload: EmailTokenRequest, request: Request):
    await _rate_limit(
        request, f"auth:verify:ip:{_request_ip(request)}", 20, 60
    )
    try:
        tenant_id, token = parse_scoped_secret(payload.token)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    verified = False
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT user_id
                FROM email_verification_tokens
                WHERE tenant_id = $1 AND token_hash = $2
                  AND used_at IS NULL AND expires_at > NOW()
                FOR UPDATE
                """,
                tenant_id,
                hash_secret(token),
            )
            if row:
                await conn.execute(
                    """
                    UPDATE users
                    SET status = 'ACTIVE', email_verified_at = NOW()
                    WHERE id = $1 AND status = 'PENDING_VERIFICATION'
                    """,
                    row["user_id"],
                )
                await conn.execute(
                    """
                    UPDATE email_verification_tokens
                    SET used_at = NOW()
                    WHERE tenant_id = $1 AND user_id = $2 AND used_at IS NULL
                    """,
                    tenant_id,
                    row["user_id"],
                )
                verified = True
    if not verified:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    return {"status": "verified", "message": "Email verified. You can now sign in."}


@router.post("/resend-verification")
async def resend_verification(payload: ForgotPasswordRequest, request: Request):
    try:
        email = normalize_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email address")

    await _rate_limit(
        request, f"auth:resend:ip:{_request_ip(request)}", 5, 3600
    )
    await _rate_limit(
        request, f"auth:resend:email:{hash_secret(email)}", 3, 3600
    )

    token_to_send = None
    async with request.app.state.db_pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, email, default_tenant_id, status
            FROM users
            WHERE lower(email) = $1
            """,
            email,
        )
        if user and user["status"] == "PENDING_VERIFICATION":
            tenant_id = user["default_tenant_id"]
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                await conn.execute(
                    """
                    UPDATE email_verification_tokens
                    SET used_at = NOW()
                    WHERE tenant_id = $1 AND user_id = $2 AND used_at IS NULL
                    """,
                    tenant_id,
                    user["id"],
                )
                token_to_send = await _create_email_token(
                    conn, tenant_id, user["id"], "email_verification_tokens"
                )
    if token_to_send:
        try:
            await send_verification_email(email, token_to_send)
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Email service temporarily unavailable",
            )
    return {
        "status": "accepted",
        "message": "If verification is pending, a new email will be sent.",
    }


@router.post("/login")
async def login(payload: LoginRequest, request: Request):
    try:
        email = normalize_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email or password")

    await _rate_limit(
        request, f"auth:login:ip:{_request_ip(request)}", 20, 60
    )
    await _rate_limit(
        request, f"auth:login:email:{hash_secret(email)}", 5, 300
    )

    async with request.app.state.db_pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, password_hash, status, default_tenant_id
            FROM users
            WHERE lower(email) = $1
            """,
            email,
        )
        stored_hash = (
            user["password_hash"]
            if user and user["password_hash"]
            else dummy_password_hash()
        )
        password_valid = await asyncio.to_thread(
            verify_password, payload.password, stored_hash
        )
        if not user or not password_valid:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user["default_tenant_id"]:
            raise HTTPException(status_code=403, detail="No active workspace membership")
        if user["status"] != "ACTIVE":
            raise HTTPException(
                status_code=403,
                detail="Verify your email address before signing in.",
            )

        tenant_id = user["default_tenant_id"]
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            membership = await conn.fetchrow(
                """
                SELECT tm.role
                FROM tenant_memberships tm
                JOIN tenants t ON t.id = tm.tenant_id
                WHERE tm.tenant_id = $1 AND tm.user_id = $2
                  AND t.status = 'ACTIVE'
                """,
                tenant_id,
                user["id"],
            )
            if not membership:
                raise HTTPException(status_code=403, detail="No active workspace membership")
            family_id = uuid4()
            refresh_token = await _issue_refresh_token(
                conn, tenant_id, user["id"], family_id
            )

    return _session_response(
        user["id"], tenant_id, family_id, membership["role"], refresh_token
    )


@router.post("/refresh")
async def refresh(payload: RefreshRequest, request: Request):
    await _rate_limit(
        request, f"auth:refresh:ip:{_request_ip(request)}", 30, 60
    )
    try:
        tenant_id, token = parse_scoped_secret(payload.refresh_token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    token_hash = hash_secret(token)
    failure = False
    result = None

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT token_hash, user_id, family_id, expires_at, revoked_at
                FROM auth_refresh_tokens
                WHERE tenant_id = $1 AND token_hash = $2
                FOR UPDATE
                """,
                tenant_id,
                token_hash,
            )
            if not row:
                failure = True
            elif row["revoked_at"] is not None:
                # Reuse of a rotated token revokes the entire token family.
                await conn.execute(
                    """
                    UPDATE auth_refresh_tokens
                    SET revoked_at = COALESCE(revoked_at, NOW())
                    WHERE tenant_id = $1 AND family_id = $2
                    """,
                    tenant_id,
                    row["family_id"],
                )
                failure = True
            elif row["expires_at"] <= _utcnow():
                await conn.execute(
                    """
                    UPDATE auth_refresh_tokens
                    SET revoked_at = COALESCE(revoked_at, NOW())
                    WHERE tenant_id = $1 AND family_id = $2
                    """,
                    tenant_id,
                    row["family_id"],
                )
                failure = True
            else:
                membership = await conn.fetchrow(
                    """
                    SELECT tm.role, u.status
                    FROM tenant_memberships tm
                    JOIN users u ON u.id = tm.user_id
                    JOIN tenants t ON t.id = tm.tenant_id
                    WHERE tm.tenant_id = $1 AND tm.user_id = $2
                      AND t.status = 'ACTIVE'
                    """,
                    tenant_id,
                    row["user_id"],
                )
                if not membership or membership["status"] != "ACTIVE":
                    await conn.execute(
                        """
                        UPDATE auth_refresh_tokens
                        SET revoked_at = COALESCE(revoked_at, NOW())
                        WHERE tenant_id = $1 AND family_id = $2
                        """,
                        tenant_id,
                        row["family_id"],
                    )
                    failure = True
                else:
                    new_token = await _issue_refresh_token(
                        conn, tenant_id, row["user_id"], row["family_id"]
                    )
                    new_hash = hash_secret(new_token)
                    await conn.execute(
                        """
                        UPDATE auth_refresh_tokens
                        SET revoked_at = NOW(), replaced_by_hash = $3
                        WHERE tenant_id = $1 AND token_hash = $2
                        """,
                        tenant_id,
                        token_hash,
                        new_hash,
                    )
                    result = (
                        row["user_id"],
                        row["family_id"],
                        membership["role"],
                        new_token,
                    )

    if failure or not result:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user_id, family_id, role, new_token = result
    return _session_response(user_id, tenant_id, family_id, role, new_token)


@router.post("/logout")
async def logout(payload: RefreshRequest, request: Request):
    await _rate_limit(
        request, f"auth:logout:ip:{_request_ip(request)}", 30, 60
    )
    try:
        tenant_id, token = parse_scoped_secret(payload.refresh_token)
    except ValueError:
        return {"status": "logged_out"}

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT family_id FROM auth_refresh_tokens
                WHERE tenant_id = $1 AND token_hash = $2
                """,
                tenant_id,
                hash_secret(token),
            )
            if row:
                await conn.execute(
                    """
                    UPDATE auth_refresh_tokens
                    SET revoked_at = COALESCE(revoked_at, NOW())
                    WHERE tenant_id = $1 AND family_id = $2
                    """,
                    tenant_id,
                    row["family_id"],
                )
    return {"status": "logged_out"}


@router.get("/me")
async def me(principal: AuthPrincipal = Depends(get_current_principal)):
    return {
        "user_id": str(principal.user_id),
        "email": principal.email,
        "tenant_id": str(principal.tenant_id),
        "role": principal.role,
        "platform_admin": principal.platform_admin,
    }


@router.post("/switch-tenant")
async def switch_tenant(
    payload: SwitchTenantRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            await conn.execute(
                """
                UPDATE auth_refresh_tokens
                SET revoked_at = COALESCE(revoked_at, NOW())
                WHERE tenant_id = $1 AND family_id = $2
                """,
                principal.tenant_id,
                principal.family_id,
            )

            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(payload.tenant_id),
            )
            membership = await conn.fetchrow(
                """
                SELECT tm.role, t.status
                FROM tenant_memberships tm
                JOIN tenants t ON t.id = tm.tenant_id
                WHERE tm.tenant_id = $1 AND tm.user_id = $2
                """,
                payload.tenant_id,
                principal.user_id,
            )
            if not membership or membership["status"] != "ACTIVE":
                raise HTTPException(status_code=403, detail="Workspace access denied")

            family_id = uuid4()
            refresh_token = await _issue_refresh_token(
                conn, payload.tenant_id, principal.user_id, family_id
            )
            await conn.execute(
                "UPDATE users SET default_tenant_id = $2 WHERE id = $1",
                principal.user_id,
                payload.tenant_id,
            )

    return _session_response(
        principal.user_id,
        payload.tenant_id,
        family_id,
        membership["role"],
        refresh_token,
    )


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, request: Request):
    try:
        email = normalize_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email address")

    await _rate_limit(
        request, f"auth:forgot:ip:{_request_ip(request)}", 10, 3600
    )
    await _rate_limit(
        request, f"auth:forgot:email:{hash_secret(email)}", 3, 3600
    )

    reset_token = None
    async with request.app.state.db_pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, email, default_tenant_id, status
            FROM users WHERE lower(email) = $1
            """,
            email,
        )
        if user and user["status"] == "ACTIVE":
            tenant_id = user["default_tenant_id"]
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                await conn.execute(
                    """
                    UPDATE password_reset_tokens
                    SET used_at = NOW()
                    WHERE tenant_id = $1 AND user_id = $2 AND used_at IS NULL
                    """,
                    tenant_id,
                    user["id"],
                )
                reset_token = await _create_email_token(
                    conn, tenant_id, user["id"], "password_reset_tokens"
                )
    if reset_token:
        try:
            await send_password_reset_email(email, reset_token)
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Email service temporarily unavailable",
            )
    return {
        "status": "accepted",
        "message": "If the account exists, a password reset email will be sent.",
    }


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest, request: Request):
    await _rate_limit(
        request, f"auth:reset:ip:{_request_ip(request)}", 10, 3600
    )
    try:
        tenant_id, token = parse_scoped_secret(payload.token)
        validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    new_hash = await asyncio.to_thread(hash_password, payload.new_password)
    updated = False
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT user_id
                FROM password_reset_tokens
                WHERE tenant_id = $1 AND token_hash = $2
                  AND used_at IS NULL AND expires_at > NOW()
                FOR UPDATE
                """,
                tenant_id,
                hash_secret(token),
            )
            if row:
                await conn.execute(
                    "UPDATE users SET password_hash = $2 WHERE id = $1",
                    row["user_id"],
                    new_hash,
                )
                await conn.execute(
                    """
                    UPDATE password_reset_tokens
                    SET used_at = NOW()
                    WHERE tenant_id = $1 AND user_id = $2 AND used_at IS NULL
                    """,
                    tenant_id,
                    row["user_id"],
                )
                await conn.execute(
                    "SELECT revoke_all_user_refresh_tokens($1)",
                    row["user_id"],
                )
                updated = True
    if not updated:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")
    return {"status": "password_reset", "message": "Password updated. Please sign in again."}
