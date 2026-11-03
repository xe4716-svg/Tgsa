from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr, constr
from app.security.proxy_vault import ProxyVault
from app.telegram.proxy_revision import proxy_revision
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.config import settings
from app.security.session_vault import SessionVault
from app.telegram.account_persistence import persist_authorized_account, finish_authorized_account
from app.telegram.authorization import lock_login_authority
from app.telegram.login_manager import LoginThrottled, LoginTransportUnavailable


class LoginRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Never echo submitted login codes or passwords in validation errors.
                return JSONResponse(status_code=422, content={"detail":"Invalid login request"})
        return wrapped


router = APIRouter(tags=["Telegram"], route_class=LoginRoute)

VALID_NOTICE_VERSIONS = {"v1.0"}


class ConnectionRequest(BaseModel):
    connection_mode: Literal["DIRECT", "PROXY"]
    proxy_profile_id: UUID | None = None
    notice_version: Literal["v1.0"]
    risk_acknowledged: Literal[True]


class PhoneConnectionRequest(ConnectionRequest):
    phone: str


class RefreshQRRequest(BaseModel):
    login_id: str


class CodeSubmitRequest(BaseModel):
    login_id: str
    code: constr(max_length=16)


class PasswordSubmitRequest(BaseModel):
    login_id: str
    password: SecretStr = Field(min_length=1, max_length=1024)


class ConsumeRequest(BaseModel):
    notice_version: str


def _get_manager(request: Request):
    return request.app.state.telegram_login_manager


def _map_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, LoginThrottled):
        return HTTPException(status_code=429, detail="Login temporarily limited",headers={"Retry-After":str(exc.retry_after)})
    if isinstance(exc, LoginTransportUnavailable):
        return HTTPException(status_code=503, detail="Telegram connection unavailable")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if isinstance(exc, TimeoutError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Flow expired")
    if isinstance(exc, asyncpg.UniqueViolationError):
        return HTTPException(status_code=409, detail="Account or proxy is already bound")
    if isinstance(exc, (asyncpg.PostgresError, OSError)):
        return HTTPException(status_code=503, detail="Service temporarily unavailable")
    if isinstance(exc, ValueError):
        msg = "Invalid login request"
        if "limit reached" in str(exc).lower():
            return HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=msg)
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")


async def _resolve_proxy_profile(request: Request, connection_mode: str, proxy_profile_id: UUID | None, tenant_id: UUID) -> dict[str, Any] | None:
    mode = connection_mode.upper()
    if mode == "DIRECT":
        if proxy_profile_id is not None:
            raise ValueError("Direct mode cannot include a proxy")
        return None
    if mode != "PROXY":
        raise ValueError("Invalid connection mode")
    if not proxy_profile_id:
        raise ValueError("Proxy profile ID is required in proxy mode")

    pool: asyncpg.Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT id, proxy_type, host, port, credentials_ciphertext, secret_version, verification_status, status
                FROM proxy_profiles
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                proxy_profile_id,
            )
            if not row:
                raise ValueError("Proxy profile not found")
            if row["status"] != "ACTIVE" or row["verification_status"] != "VERIFIED":
                raise ValueError("Proxy must be verified and active")
            profile = dict(row)
            profile["_revision"] = proxy_revision(profile)
            ciphertext = profile.pop("credentials_ciphertext")
            if ciphertext:
                vault = ProxyVault.from_base64(settings.session_kek_base64, settings.session_kek_id)
                profile.update(vault.decode_credentials(tenant_id, proxy_profile_id, ciphertext))
            return profile


async def _record_consent(request, principal, body):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            await lock_login_authority(conn, principal.tenant_id, principal.user_id, principal.family_id)
            await conn.execute(
                """INSERT INTO connection_consents
                   (tenant_id,user_id,connection_mode,proxy_profile_id,notice_version)
                   VALUES ($1,$2,$3,$4,$5)""",
                principal.tenant_id, principal.user_id, body.connection_mode,
                body.proxy_profile_id, body.notice_version,
            )


async def _write_principal(principal: AuthPrincipal = Depends(get_current_principal)):
    if principal.role == "VIEWER":
        raise HTTPException(status_code=403, detail="Read-only membership")
    return principal


@router.post("/telegram/login/qr")
async def start_qr(
    request: Request,
    body: ConnectionRequest,
    principal: AuthPrincipal = Depends(_write_principal),
):
    try:
        proxy_profile = await _resolve_proxy_profile(
            request, body.connection_mode, body.proxy_profile_id, principal.tenant_id
        )
        await _record_consent(request, principal, body)
        manager = _get_manager(request)
        return await manager.start_qr(
            str(principal.tenant_id),
            str(principal.user_id),
            connection_mode=body.connection_mode,
            proxy_profile=proxy_profile,
            notice_version=body.notice_version,
        )
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/qr/refresh")
async def refresh_qr(
    request: Request,
    body: RefreshQRRequest,
    principal: AuthPrincipal = Depends(_write_principal),
):
    try:
        manager = _get_manager(request)
        async with manager.acquire_login_lock(body.login_id, str(principal.tenant_id), str(principal.user_id)):
            return await manager.refresh_qr(body.login_id, str(principal.tenant_id), str(principal.user_id))
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/phone")
async def start_phone(
    request: Request,
    body: PhoneConnectionRequest,
    principal: AuthPrincipal = Depends(_write_principal),
):
    try:
        proxy_profile = await _resolve_proxy_profile(
            request, body.connection_mode, body.proxy_profile_id, principal.tenant_id
        )
        await _record_consent(request, principal, body)
        manager = _get_manager(request)
        return await manager.start_phone(
            str(principal.tenant_id),
            str(principal.user_id),
            body.phone,
            connection_mode=body.connection_mode,
            proxy_profile=proxy_profile,
            notice_version=body.notice_version,
        )
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/phone/code")
async def submit_phone_code(
    request: Request,
    body: CodeSubmitRequest,
    principal: AuthPrincipal = Depends(_write_principal),
):
    try:
        manager = _get_manager(request)
        async with manager.acquire_login_lock(body.login_id, str(principal.tenant_id), str(principal.user_id)):
            return await manager.submit_phone_code(
                body.login_id, str(principal.tenant_id), str(principal.user_id), body.code
            )
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/2fa")
async def submit_2fa(
    request: Request,
    body: PasswordSubmitRequest,
    principal: AuthPrincipal = Depends(_write_principal),
):
    try:
        manager = _get_manager(request)
        async with manager.acquire_login_lock(body.login_id, str(principal.tenant_id), str(principal.user_id)):
            return await manager.submit_2fa_password(
                body.login_id, str(principal.tenant_id), str(principal.user_id), body.password.get_secret_value()
            )
    except Exception as exc:
        raise _map_exception(exc)


@router.get("/telegram/login/{login_id}/status")
async def get_login_status(
    request: Request,
    login_id: str,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    try:
        manager = _get_manager(request)
        return manager.get_status(login_id, str(principal.tenant_id), str(principal.user_id))
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/{login_id}/cancel")
async def cancel_login(
    request: Request,
    login_id: str,
    principal: AuthPrincipal = Depends(_write_principal),
):
    try:
        manager = _get_manager(request)
        await manager.cancel(login_id, str(principal.tenant_id), str(principal.user_id))
        return {"success": True}
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/{login_id}/consume")
async def consume_login(
    request: Request,
    login_id: str,
    body: ConsumeRequest,
    principal: AuthPrincipal = Depends(_write_principal),
):
    if body.notice_version not in VALID_NOTICE_VERSIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid notice version")

    manager = _get_manager(request)

    try:
        async with manager.acquire_login_lock(login_id, str(principal.tenant_id), str(principal.user_id)):
            auth_data = await manager.consume_authorized(
                login_id, str(principal.tenant_id), str(principal.user_id)
            )

            if "account_id" in auth_data:
                if await manager.confirm_disconnected(login_id,str(principal.tenant_id),str(principal.user_id)):
                    await finish_authorized_account(request.app.state.db_pool,tenant_id=principal.tenant_id,
                        user_id=principal.user_id,family_id=principal.family_id,account_id=UUID(auth_data["account_id"]))
                return {"account_id": auth_data["account_id"]}

            if auth_data["notice_version"] != body.notice_version:
                raise ValueError("Connection notice changed; restart authorization")
            vault = SessionVault.from_base64(settings.session_kek_base64, settings.session_kek_id)
            pool = request.app.state.db_pool

            proxy_profile = await _resolve_proxy_profile(
                request,
                auth_data["connection_mode"],
                UUID(auth_data["proxy_profile_id"]) if auth_data["proxy_profile_id"] else None,
                principal.tenant_id
            )

            if proxy_profile and proxy_profile["_revision"] != auth_data["proxy_revision"]:
                raise ValueError("Proxy configuration changed; restart authorization")
            account_id = await persist_authorized_account(
                pool,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                family_id=principal.family_id,
                account=auth_data["account"],
                session_string=auth_data["session_string"],
                vault=vault,
                connection_mode=auth_data["connection_mode"],
                proxy_profile=proxy_profile,
                proxy_profile_id=UUID(auth_data["proxy_profile_id"]) if auth_data["proxy_profile_id"] else None,
                notice_version=body.notice_version,
            )

            closed=await manager.mark_consumed(
                login_id, str(principal.tenant_id), str(principal.user_id), str(account_id)
            )
            if closed:
                await finish_authorized_account(pool,tenant_id=principal.tenant_id,user_id=principal.user_id,
                    family_id=principal.family_id,account_id=account_id)

            return {"account_id": str(account_id)}
    except Exception as exc:
        raise _map_exception(exc)
