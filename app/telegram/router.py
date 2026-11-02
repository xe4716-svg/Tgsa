from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, constr

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.config import settings
from app.security.session_vault import SessionVault
from app.telegram.account_persistence import persist_authorized_account


router = APIRouter(tags=["Telegram"])

VALID_NOTICE_VERSIONS = {"v1.0"}


class ConnectionRequest(BaseModel):
    connection_mode: str
    proxy_profile_id: UUID | None = None


class PhoneConnectionRequest(ConnectionRequest):
    phone: str


class RefreshQRRequest(BaseModel):
    login_id: str


class CodeSubmitRequest(BaseModel):
    login_id: str
    code: constr(max_length=16)


class PasswordSubmitRequest(BaseModel):
    login_id: str
    password: constr(max_length=1024)


class ConsumeRequest(BaseModel):
    notice_version: str


def _get_manager(request: Request):
    return request.app.state.telegram_login_manager


def _map_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if isinstance(exc, TimeoutError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Flow expired")
    if isinstance(exc, ValueError):
        msg = str(exc)
        if "limit reached" in msg.lower():
            return HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=msg)
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")


async def _resolve_proxy_profile(request: Request, connection_mode: str, proxy_profile_id: UUID | None, tenant_id: UUID) -> dict[str, Any] | None:
    mode = connection_mode.upper()
    if mode != "PROXY":
        return None
    if not proxy_profile_id:
        raise ValueError("Proxy profile ID is required in proxy mode")

    pool: asyncpg.Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, proxy_type, host, port, username, password, secret, verification_status, status
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
        return dict(row)


@router.post("/telegram/login/qr")
async def start_qr(
    request: Request,
    body: ConnectionRequest,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    try:
        proxy_profile = await _resolve_proxy_profile(
            request, body.connection_mode, body.proxy_profile_id, principal.tenant_id
        )
        manager = _get_manager(request)
        return await manager.start_qr(
            str(principal.tenant_id),
            str(principal.user_id),
            connection_mode=body.connection_mode,
            proxy_profile=proxy_profile,
        )
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/qr/refresh")
async def refresh_qr(
    request: Request,
    body: RefreshQRRequest,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    try:
        manager = _get_manager(request)
        return await manager.refresh_qr(body.login_id, str(principal.tenant_id), str(principal.user_id))
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/phone")
async def start_phone(
    request: Request,
    body: PhoneConnectionRequest,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    try:
        proxy_profile = await _resolve_proxy_profile(
            request, body.connection_mode, body.proxy_profile_id, principal.tenant_id
        )
        manager = _get_manager(request)
        return await manager.start_phone(
            str(principal.tenant_id),
            str(principal.user_id),
            body.phone,
            connection_mode=body.connection_mode,
            proxy_profile=proxy_profile,
        )
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/phone/code")
async def submit_phone_code(
    request: Request,
    body: CodeSubmitRequest,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    try:
        manager = _get_manager(request)
        return await manager.submit_phone_code(
            body.login_id, str(principal.tenant_id), str(principal.user_id), body.code
        )
    except Exception as exc:
        raise _map_exception(exc)


@router.post("/telegram/login/2fa")
async def submit_2fa(
    request: Request,
    body: PasswordSubmitRequest,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    try:
        manager = _get_manager(request)
        return await manager.submit_2fa_password(
            body.login_id, str(principal.tenant_id), str(principal.user_id), body.password
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
    principal: AuthPrincipal = Depends(get_current_principal),
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
    principal: AuthPrincipal = Depends(get_current_principal),
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
                return {"account_id": auth_data["account_id"]}

            vault = SessionVault.from_base64(settings.session_kek_base64, settings.session_kek_id)
            pool = request.app.state.db_pool

            proxy_profile = await _resolve_proxy_profile(
                request,
                auth_data["connection_mode"],
                UUID(auth_data["proxy_profile_id"]) if auth_data["proxy_profile_id"] else None,
                principal.tenant_id
            )

            account_id = await persist_authorized_account(
                pool,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                account=auth_data["account"],
                session_string=auth_data["session_string"],
                vault=vault,
                connection_mode=auth_data["connection_mode"],
                proxy_profile=proxy_profile,
                proxy_profile_id=UUID(auth_data["proxy_profile_id"]) if auth_data["proxy_profile_id"] else None,
                notice_version=body.notice_version,
            )

            await manager.mark_consumed(
                login_id, str(principal.tenant_id), str(principal.user_id), str(account_id)
            )

            return {"account_id": str(account_id)}
    except Exception as exc:
        raise _map_exception(exc)
