from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import Any

from app.telegram.client_factory import create_client


PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
QR_WAIT_SECONDS = 25


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 5:
        return "****"
    return f"+{digits[:2]}{'*' * max(3, len(digits) - 4)}{digits[-2:]}"


@dataclass
class PendingLogin:
    login_id: str
    tenant_id: str
    user_id: str
    method: str
    client: Any
    created_at: float
    expires_at: float
    status: str
    phone: str | None = None
    phone_code_hash: str | None = None
    qr_login: Any | None = None
    wait_task: asyncio.Task | None = None
    session_string: str | None = None
    account: dict[str, Any] | None = None
    error_code: str | None = None
    connection_mode: str | None = None
    proxy_profile_id: str | None = None
    account_id: str | None = None
    proxy_revision: str | None = None
    notice_version: str = "v1.0"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TelegramLoginManager:
    """Single-process login coordinator for QR and phone-code sign-in.

    Bind each login_id to the authenticated tenant/user that created it.
    Pending Telethon clients are kept in memory, not Redis. In production,
    route a login flow to its owning account-gateway worker.
    Never log login codes, 2FA passwords, QR tokens, or session strings.
    """

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._pending: dict[str, PendingLogin] = {}
        self._lock = asyncio.Lock()

    async def start_qr(
        self, tenant_id: str, user_id: str,
        *,
        connection_mode: str,
        proxy_profile: dict[str, Any] | None = None,
        notice_version: str = "v1.0",
    ) -> dict[str, Any]:
        client = create_client(
            connection_mode=connection_mode,
            proxy_profile=proxy_profile,
        )
        try:
            await client.connect()
            qr = await client.qr_login()
        except Exception:
            await client.disconnect()
            raise

        now = time.time()
        login_id = secrets.token_urlsafe(24)
        pending = PendingLogin(
            login_id=login_id,
            tenant_id=tenant_id,
            user_id=user_id,
            method="QR",
            client=client,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            status="QR_PENDING",
            qr_login=qr,
            lock=asyncio.Lock(),
            notice_version=notice_version,
            connection_mode=connection_mode,
            proxy_profile_id=str(proxy_profile["id"]) if proxy_profile else None,
            proxy_revision=proxy_profile.get("_revision") if proxy_profile else None,
        )
        async with self._lock:
            self._pending[login_id] = pending
        pending.wait_task = asyncio.create_task(self._wait_for_qr(login_id))
        return {
            "login_id": login_id,
            "qr_url": qr.url,
            "status": pending.status,
            "expires_in": self.ttl_seconds,
            "qr_refresh_in": QR_WAIT_SECONDS,
        }

    async def _wait_for_qr(self, login_id: str) -> None:
        pending = self._pending.get(login_id)
        if not pending:
            return
        try:
            user = await pending.qr_login.wait(timeout=QR_WAIT_SECONDS)
            await self._mark_authorized(pending, user)
        except Exception as exc:
            from telethon.errors import SessionPasswordNeededError
            if isinstance(exc, SessionPasswordNeededError):
                pending.status = "PASSWORD_REQUIRED"
                return
            if isinstance(exc, asyncio.TimeoutError):
                # QR token expires quickly; the overall login flow remains alive
                # so the UI can request a fresh QR token.
                pending.status = "QR_EXPIRED"
                return
            pending.status = "FAILED"
            pending.error_code = "QR_LOGIN_FAILED"
            await pending.client.disconnect()

    async def refresh_qr(
        self, login_id: str, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        pending = self._get_owned(login_id, tenant_id, user_id)
        if pending.method != "QR" or pending.status != "QR_EXPIRED":
            raise ValueError("QR code is not ready to refresh")
        await pending.qr_login.recreate()
        pending.status = "QR_PENDING"
        pending.error_code = None
        pending.wait_task = asyncio.create_task(self._wait_for_qr(login_id))
        return {
            "login_id": login_id,
            "qr_url": pending.qr_login.url,
            "status": pending.status,
            "qr_refresh_in": QR_WAIT_SECONDS,
        }

    async def start_phone(
        self, tenant_id: str, user_id: str, phone: str,
        *,
        connection_mode: str,
        proxy_profile: dict[str, Any] | None = None,
        notice_version: str = "v1.0",
    ) -> dict[str, Any]:
        if not PHONE_RE.fullmatch(phone):
            raise ValueError("Phone number must use international E.164 format")
        client = create_client(
            connection_mode=connection_mode,
            proxy_profile=proxy_profile,
        )
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise

        now = time.time()
        login_id = secrets.token_urlsafe(24)
        pending = PendingLogin(
            login_id=login_id,
            tenant_id=tenant_id,
            user_id=user_id,
            method="PHONE",
            client=client,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            status="CODE_REQUIRED",
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            lock=asyncio.Lock(),
            notice_version=notice_version,
            connection_mode=connection_mode,
            proxy_profile_id=str(proxy_profile["id"]) if proxy_profile else None,
            proxy_revision=proxy_profile.get("_revision") if proxy_profile else None,
        )
        async with self._lock:
            self._pending[login_id] = pending
        return {
            "login_id": login_id,
            "status": pending.status,
            "expires_in": self.ttl_seconds,
        }

    def _get_owned(self, login_id: str, tenant_id: str, user_id: str) -> PendingLogin:
        pending = self._pending.get(login_id)
        if not pending or pending.tenant_id != tenant_id or pending.user_id != user_id:
            raise PermissionError("Login flow not found")
        if time.time() > pending.expires_at:
            pending.status = "EXPIRED"
            raise TimeoutError("Login flow expired")
        return pending

    async def submit_phone_code(
        self, login_id: str, tenant_id: str, user_id: str, code: str
    ) -> dict[str, Any]:
        pending = self._get_owned(login_id, tenant_id, user_id)
        if pending.method != "PHONE" or pending.status not in ("CODE_REQUIRED", "CODE_REJECTED"):
            raise ValueError("This login flow is not waiting for a phone code")
        if not code or len(code) > 16:
            raise ValueError("Invalid verification code")
        try:
            user = await pending.client.sign_in(
                phone=pending.phone,
                code=code,
                phone_code_hash=pending.phone_code_hash,
            )
            await self._mark_authorized(pending, user)
        except Exception as exc:
            from telethon.errors import SessionPasswordNeededError
            if isinstance(exc, SessionPasswordNeededError):
                pending.status = "PASSWORD_REQUIRED"
            else:
                pending.status = "CODE_REJECTED"
                pending.error_code = "PHONE_CODE_REJECTED"
        return {"login_id": login_id, "status": pending.status}

    async def submit_2fa_password(
        self, login_id: str, tenant_id: str, user_id: str, password: str
    ) -> dict[str, Any]:
        pending = self._get_owned(login_id, tenant_id, user_id)
        if pending.status not in ("PASSWORD_REQUIRED", "PASSWORD_REJECTED"):
            raise ValueError("This login flow is not waiting for a 2FA password")
        if not password or len(password) > 1024:
            raise ValueError("Invalid 2FA password")
        try:
            user = await pending.client.sign_in(password=password)
            await self._mark_authorized(pending, user)
        except Exception:
            pending.status = "PASSWORD_REJECTED"
            pending.error_code = "TWO_FACTOR_PASSWORD_REJECTED"
        return {"login_id": login_id, "status": pending.status}

    async def _mark_authorized(self, pending: PendingLogin, user: Any) -> None:
        pending.session_string = pending.client.session.save()
        phone = getattr(user, "phone", None)
        pending.account = {
            "telegram_user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone_masked": _mask_phone(phone),
        }
        pending.status = "AUTHORIZED"

    def get_status(self, login_id: str, tenant_id: str, user_id: str) -> dict[str, Any]:
        pending = self._get_owned(login_id, tenant_id, user_id)
        return {
            "login_id": login_id,
            "method": pending.method,
            "status": pending.status,
            "account": pending.account if pending.status in ("AUTHORIZED", "CONSUMED") else None,
            "error_code": pending.error_code,
        }

    @asynccontextmanager
    async def acquire_login_lock(self, login_id: str, tenant_id: str, user_id: str):
        pending = self._get_owned(login_id, tenant_id, user_id)
        async with pending.lock:
            self._get_owned(login_id, tenant_id, user_id)
            yield

    async def consume_authorized(
        self, login_id: str, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        pending = self._get_owned(login_id, tenant_id, user_id)
        if pending.status == "CONSUMED" and pending.account_id:
            return {"account_id": pending.account_id}
        if pending.status != "AUTHORIZED" or not pending.session_string:
            raise ValueError("Login has not been authorized")
        return {
            "session_string": pending.session_string,
            "account": pending.account,
            "connection_mode": pending.connection_mode,
            "proxy_profile_id": pending.proxy_profile_id,
            "notice_version": pending.notice_version,
            "proxy_revision": pending.proxy_revision,
        }

    async def mark_consumed(self, login_id: str, tenant_id: str, user_id: str, account_id: str) -> None:
        # The caller holds the flow lock. A database commit remains successful
        # even if the login TTL elapsed while PostgreSQL was completing it.
        pending = self._pending.get(login_id)
        if not pending or pending.tenant_id != tenant_id or pending.user_id != user_id:
            raise PermissionError("Login flow not found")
        if pending.status != "AUTHORIZED":
            return
        pending.status = "CONSUMED"
        pending.account_id = account_id
        pending.session_string = None
        try:
            await asyncio.wait_for(pending.client.disconnect(), timeout=5)
        except Exception:
            pass  # Persisted successfully; disconnect failure must not repeat the write.

    async def _dispose(self, pending: PendingLogin) -> None:
        async with pending.lock:
            task = pending.wait_task
            if task and not task.done():
                task.cancel()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            pending.session_string = None
            try:
                await asyncio.wait_for(pending.client.disconnect(), timeout=5)
            except Exception:
                pass
            finally:
                self._pending.pop(pending.login_id, None)

    async def cancel(self, login_id: str, tenant_id: str, user_id: str) -> None:
        pending = self._get_owned(login_id, tenant_id, user_id)
        await self._dispose(pending)

    async def cleanup_expired(self) -> int:
        expired = [item for item in list(self._pending.values()) if item.expires_at < time.time()]
        await asyncio.gather(*(self._dispose(item) for item in expired))
        return len(expired)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self._dispose(item) for item in list(self._pending.values())))
