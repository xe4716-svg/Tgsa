from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.auth.mailer import send_billing_notification_email
from app.billing.commerce import PurchaseRequest, purchase_membership
from app.config import settings


logger = logging.getLogger(__name__)
router = APIRouter(tags=["billing automation"])
_BILLING_AUTOMATION_LOCK_ID = 73190528


class AutoRenewUpdateRequest(BaseModel):
    enabled: bool
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    timezone: str | None = Field(default=None, max_length=80)


class BillingNotificationPreferencesRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=80)
    email_notifications_enabled: bool = True


class BillingAutomationSettingsRequest(BaseModel):
    renewal_advance_days: int = Field(ge=1, le=7)
    retry_interval_minutes: int = Field(ge=5, le=1440)
    max_technical_retries: int = Field(ge=1, le=3)
    notification_timezone_mode: str
    platform_timezone: str = Field(min_length=1, max_length=80)
    price_change_notice_hours: int = Field(ge=1, le=168)
    reminder_days: list[int] = Field(min_length=1, max_length=12)


def _validate_timezone(value: str) -> str:
    candidate = value.strip()
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Unknown IANA timezone") from exc
    return candidate


def _require_tenant_admin(principal: AuthPrincipal) -> None:
    if principal.role not in {"OWNER", "ADMIN"} and not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Workspace owner or administrator permission required")


def _require_platform_admin(principal: AuthPrincipal) -> None:
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator permission required")


def _normalize_currency(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"CNY", "USD", "USDT"}:
        raise HTTPException(status_code=422, detail="Supported currencies are CNY, USD and USDT")
    return normalized


def _format_amount(currency: str, amount_minor: int) -> str:
    decimals = 6 if currency == "USDT" else 2
    whole, fraction = divmod(int(amount_minor), 10 ** decimals)
    return f"{currency} {whole:,}.{fraction:0{decimals}d}"


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if value is not None else {}


def reminder_bucket(expires_at: datetime, now: datetime, reminder_days: list[int]) -> int | None:
    """Return the configured reminder bucket whose one-day window contains expiry."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    remaining_days = (expires_at - now).total_seconds() / 86400
    for day in sorted(set(reminder_days), reverse=True):
        if day == 0:
            if -1 < remaining_days <= 0:
                return 0
        elif day - 1 < remaining_days <= day:
            return day
    return None


async def _notify_user(
    conn,
    *,
    tenant_id: UUID,
    user_id: UUID,
    dedupe_key: str,
    event_type: str,
    title: str,
    body: str,
    payload: dict,
    email_enabled: bool = True,
    recipient_email: str | None = None,
) -> bool:
    notification_id = await conn.fetchval(
        """
        INSERT INTO tenant_notifications (
            tenant_id, user_id, dedupe_key, event_type, title, body, payload
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (tenant_id, user_id, dedupe_key) DO NOTHING
        RETURNING id
        """,
        tenant_id,
        user_id,
        dedupe_key[:160],
        event_type,
        title[:160],
        body,
        json.dumps(payload, default=str),
    )
    if not notification_id:
        return False

    if email_enabled and recipient_email:
        await conn.execute(
            """
            INSERT INTO billing_email_outbox (
                tenant_id, user_id, dedupe_key, subject, body
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
            """,
            tenant_id,
            user_id,
            dedupe_key[:160],
            title[:200],
            body,
        )

    telegram_linked = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM tenant_telegram_notification_links
            WHERE tenant_id = $1 AND user_id = $2
        )
        """,
        tenant_id,
        user_id,
    )
    if telegram_linked:
        await conn.execute(
            """
            INSERT INTO telegram_notification_outbox (
                tenant_id, user_id, dedupe_key, message_text
            )
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
            """,
            tenant_id,
            user_id,
            dedupe_key[:160],
            (title + "\n\n" + body)[:4096],
        )
    return True


async def _tenant_email_enabled(conn, tenant_id: UUID) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT COALESCE(
            (SELECT email_notifications_enabled
             FROM tenant_billing_preferences
             WHERE tenant_id = $1),
            TRUE
        )
        """,
        tenant_id,
    ))


@router.get("/billing/auto-renew")
async def get_auto_renew_settings(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            row = await conn.fetchrow(
                """
                SELECT ar.enabled, ar.currency, ar.duration_days, ar.timezone,
                       ar.last_price_minor, ar.last_price_version,
                       s.expires_at, p.code AS plan_code, p.name AS plan_name,
                       ri.status AS renewal_status,
                       ri.next_attempt_at AS renewal_next_attempt_at,
                       ri.last_error_code AS renewal_last_error_code
                FROM tenant_auto_renew_settings ar
                JOIN tenant_subscriptions s
                  ON s.tenant_id = ar.tenant_id AND s.id = ar.subscription_id
                JOIN membership_plans p ON p.id = s.plan_id
                LEFT JOIN LATERAL (
                    SELECT status, next_attempt_at, last_error_code
                    FROM subscription_renewal_intents
                    WHERE tenant_id = ar.tenant_id
                      AND subscription_id = ar.subscription_id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) ri ON TRUE
                WHERE ar.tenant_id = $1
                """,
                principal.tenant_id,
            )
    if not row:
        return {"enabled": False, "currency": None, "duration_days": None, "timezone": "UTC"}
    return {
        "enabled": row["enabled"],
        "currency": row["currency"],
        "duration_days": row["duration_days"],
        "timezone": row["timezone"],
        "plan_code": row["plan_code"],
        "plan_name": row["plan_name"],
        "expires_at": row["expires_at"],
        "last_price_minor": row["last_price_minor"],
        "last_price_version": row["last_price_version"],
        "renewal_status": row["renewal_status"],
        "renewal_next_attempt_at": row["renewal_next_attempt_at"],
        "renewal_last_error_code": row["renewal_last_error_code"],
    }


@router.put("/billing/auto-renew")
async def update_auto_renew_settings(
    payload: AutoRenewUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_tenant_admin(principal)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            tenant = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                principal.tenant_id,
            )
            if not tenant:
                raise HTTPException(status_code=404, detail="Workspace not found")

            if not payload.enabled:
                await conn.execute(
                    """
                    UPDATE tenant_auto_renew_settings
                    SET enabled = FALSE, updated_by = $2, updated_at = NOW()
                    WHERE tenant_id = $1
                    """,
                    principal.tenant_id,
                    principal.user_id,
                )
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET status = 'CANCELED', updated_at = NOW(),
                        last_error_code = 'AUTO_RENEW_DISABLED'
                    WHERE tenant_id = $1
                      AND status IN ('PENDING', 'PROCESSING', 'WAITING_FOR_BALANCE', 'RETRY', 'FAILED')
                    """,
                    principal.tenant_id,
                )
                await conn.execute(
                    """
                    INSERT INTO audit_logs (
                        tenant_id, actor_user_id, action, resource_type, details
                    )
                    VALUES ($1, $2, 'AUTO_RENEW_DISABLED', 'auto_renew_settings', '{}'::jsonb)
                    """,
                    principal.tenant_id,
                    principal.user_id,
                )
                return {"enabled": False}

            if not payload.currency or payload.duration_days is None:
                raise HTTPException(
                    status_code=422,
                    detail="Currency and renewal duration are required when enabling auto-renewal",
                )
            currency = _normalize_currency(payload.currency)
            saved_timezone = await conn.fetchval(
                "SELECT timezone FROM tenant_billing_preferences WHERE tenant_id = $1",
                principal.tenant_id,
            )
            timezone_value = payload.timezone or saved_timezone
            if not timezone_value:
                raise HTTPException(
                    status_code=422,
                    detail="Choose a timezone in notification preferences before enabling auto-renewal",
                )
            timezone_name = _validate_timezone(timezone_value)

            current = await conn.fetchrow(
                """
                SELECT s.id, s.plan_id, s.status, s.expires_at,
                       p.code AS plan_code, p.name AS plan_name
                FROM tenant_subscriptions s
                JOIN membership_plans p ON p.id = s.plan_id
                WHERE s.tenant_id = $1
                  AND s.starts_at <= NOW()
                  AND s.status IN ('ACTIVE', 'TRIALING')
                  AND s.expires_at > NOW()
                ORDER BY s.starts_at DESC
                LIMIT 1
                FOR UPDATE OF s
                """,
                principal.tenant_id,
            )
            if not current or current["plan_code"] == "FREE":
                raise HTTPException(status_code=409, detail="An active paid, time-limited membership is required")
            currencies = await conn.fetch(
                """
                SELECT DISTINCT currency
                FROM subscription_terms
                WHERE tenant_id = $1 AND status = 'ACTIVE'
                  AND ends_at > NOW() AND net_paid_minor > 0
                """,
                principal.tenant_id,
            )
            if any(row["currency"] != currency for row in currencies):
                raise HTTPException(
                    status_code=409,
                    detail="Auto-renewal must use the currency of the remaining paid membership value",
                )
            price = await conn.fetchrow(
                """
                SELECT amount_minor, version
                FROM membership_plan_prices
                WHERE plan_id = $1 AND currency = $2
                  AND duration_days = $3 AND is_current = TRUE
                """,
                current["plan_id"],
                currency,
                payload.duration_days,
            )
            if not price:
                raise HTTPException(status_code=404, detail="No current price exists for this renewal option")

            await conn.execute(
                """
                INSERT INTO tenant_billing_preferences (
                    tenant_id, timezone, timezone_confirmed_at, updated_by
                )
                VALUES ($1, $2, NOW(), $3)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    timezone = EXCLUDED.timezone,
                    timezone_confirmed_at = NOW(),
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                principal.tenant_id,
                timezone_name,
                principal.user_id,
            )
            await conn.execute(
                """
                INSERT INTO tenant_auto_renew_settings (
                    tenant_id, subscription_id, user_id, enabled, currency,
                    duration_days, timezone, enabled_at, last_price_minor,
                    last_price_version, updated_by
                )
                VALUES ($1, $2, $3, TRUE, $4, $5, $6, NOW(), $7, $8, $3)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    subscription_id = EXCLUDED.subscription_id,
                    user_id = EXCLUDED.user_id,
                    enabled = TRUE,
                    currency = EXCLUDED.currency,
                    duration_days = EXCLUDED.duration_days,
                    timezone = EXCLUDED.timezone,
                    enabled_at = NOW(),
                    last_price_minor = EXCLUDED.last_price_minor,
                    last_price_version = EXCLUDED.last_price_version,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                principal.tenant_id,
                current["id"],
                principal.user_id,
                currency,
                payload.duration_days,
                timezone_name,
                price["amount_minor"],
                price["version"],
            )
            await conn.execute(
                """
                UPDATE subscription_renewal_intents
                SET status = 'CANCELED', updated_at = NOW(),
                    last_error_code = 'AUTO_RENEW_SETTINGS_CHANGED'
                WHERE tenant_id = $1
                  AND status IN ('PENDING', 'PROCESSING', 'WAITING_FOR_BALANCE', 'RETRY', 'FAILED')
                """,
                principal.tenant_id,
            )
            await conn.execute(
                """
                INSERT INTO audit_logs (
                    tenant_id, actor_user_id, action, resource_type, details
                )
                VALUES ($1, $2, 'AUTO_RENEW_ENABLED', 'auto_renew_settings', $3::jsonb)
                """,
                principal.tenant_id,
                principal.user_id,
                json.dumps({
                    "plan_code": current["plan_code"],
                    "currency": currency,
                    "duration_days": payload.duration_days,
                    "price_minor": price["amount_minor"],
                    "price_version": price["version"],
                    "timezone": timezone_name,
                }),
            )
    return {
        "enabled": True,
        "plan_code": current["plan_code"],
        "plan_name": current["plan_name"],
        "currency": currency,
        "duration_days": payload.duration_days,
        "price_minor": price["amount_minor"],
        "price_version": price["version"],
        "expires_at": current["expires_at"],
        "timezone": timezone_name,
    }


@router.get("/billing/notification-preferences")
async def get_billing_notification_preferences(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            row = await conn.fetchrow(
                """
                SELECT timezone, email_notifications_enabled,
                       telegram_notifications_enabled, timezone_confirmed_at,
                       EXISTS (
                           SELECT 1 FROM tenant_telegram_notification_links l
                           WHERE l.tenant_id = tenant_billing_preferences.tenant_id
                             AND l.user_id = $2
                       ) AS telegram_linked
                FROM tenant_billing_preferences
                WHERE tenant_id = $1
                """,
                principal.tenant_id,
                principal.user_id,
            )
    return {
        "timezone": row["timezone"] if row else "UTC",
        "email_notifications_enabled": row["email_notifications_enabled"] if row else True,
        "telegram_notifications_enabled": row["telegram_notifications_enabled"] if row else False,
        "telegram_linked": row["telegram_linked"] if row else False,
        "timezone_confirmed_at": row["timezone_confirmed_at"] if row else None,
    }


@router.get("/billing/telegram-link")
async def get_telegram_notification_link(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            row = await conn.fetchrow(
                """
                SELECT telegram_user_id, username, linked_at
                FROM tenant_telegram_notification_links
                WHERE tenant_id = $1 AND user_id = $2
                """,
                principal.tenant_id,
                principal.user_id,
            )
    return {
        "linked": bool(row),
        "username": row["username"] if row else None,
        "linked_at": row["linked_at"] if row else None,
    }


@router.post("/billing/telegram-link")
async def create_telegram_notification_link(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not (
        settings.platform_telegram_bot_token
        and settings.platform_telegram_bot_username
        and settings.platform_telegram_webhook_secret
    ):
        raise HTTPException(status_code=503, detail="Platform Telegram notification bot is not configured")
    raw_token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            await conn.execute(
                """
                DELETE FROM tenant_telegram_link_tokens
                WHERE tenant_id = $1 AND user_id = $2
                  AND (used_at IS NOT NULL OR expires_at <= NOW())
                """,
                principal.tenant_id,
                principal.user_id,
            )
            await conn.execute(
                """
                INSERT INTO tenant_telegram_link_tokens (
                    token_hash, tenant_id, user_id, expires_at
                )
                VALUES ($1, $2, $3, NOW() + INTERVAL '15 minutes')
                """,
                token_hash,
                principal.tenant_id,
                principal.user_id,
            )
            expires_at = await conn.fetchval(
                """
                SELECT expires_at FROM tenant_telegram_link_tokens
                WHERE token_hash = $1
                """,
                token_hash,
            )
    username = settings.platform_telegram_bot_username.strip().lstrip("@")
    return {
        "bot_username": username,
        "deep_link": f"https://t.me/{username}?start={raw_token}",
        "expires_at": expires_at,
        "expires_in_seconds": 900,
    }


@router.delete("/billing/telegram-link")
async def remove_telegram_notification_link(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            await conn.execute(
                """
                DELETE FROM tenant_telegram_notification_links
                WHERE tenant_id = $1 AND user_id = $2
                """,
                principal.tenant_id,
                principal.user_id,
            )
            await conn.execute(
                """
                UPDATE tenant_billing_preferences
                SET telegram_notifications_enabled = EXISTS (
                        SELECT 1 FROM tenant_telegram_notification_links
                        WHERE tenant_id = $1
                    ),
                    updated_at = NOW()
                WHERE tenant_id = $1
                """,
                principal.tenant_id,
            )
            await conn.execute(
                """
                UPDATE telegram_notification_outbox
                SET status = 'FAILED',
                    last_error_code = 'TELEGRAM_LINK_REMOVED',
                    updated_at = NOW()
                WHERE tenant_id = $1 AND user_id = $2
                  AND status IN ('PENDING', 'PROCESSING')
                """,
                principal.tenant_id,
                principal.user_id,
            )
    return {"linked": False}


@router.post("/webhooks/platform-telegram")
async def platform_telegram_webhook(request: Request):
    expected_secret = settings.platform_telegram_webhook_secret
    if not (
        settings.platform_telegram_bot_token
        and expected_secret
    ):
        raise HTTPException(status_code=503, detail="Platform Telegram webhook is not configured")
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied_secret, expected_secret):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > 1_048_576:
                raise HTTPException(status_code=413, detail="Telegram update is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length") from exc
    raw = await request.body()
    if len(raw) > 1_048_576:
        raise HTTPException(status_code=413, detail="Telegram update is too large")
    try:
        update = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update JSON") from exc

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    text_value = message.get("text") or ""
    if chat.get("type") != "private" or not sender.get("id"):
        return {"ok": True}

    telegram_user_id = int(sender["id"])
    chat_id = int(chat["id"])
    parts = text_value.split(maxsplit=1)
    command = parts[0].split("@", 1)[0] if parts else ""

    reply = None
    if command == "/start":
        if len(parts) == 2:
            raw_token = parts[1].strip()
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            async with request.app.state.db_pool.acquire() as conn:
                linked = await conn.fetchval(
                    """
                    SELECT consume_telegram_link_token($1::char(64), $2, $3, $4)
                    """,
                    token_hash,
                    telegram_user_id,
                    chat_id,
                    sender.get("username"),
                )
            reply = (
                "TGMate 通知已成功绑定。你可以在账户设置中随时解除绑定。"
                if linked
                else "绑定链接无效或已过期，请返回 TGMate 重新生成绑定链接。"
            )
        else:
            reply = "请先在 TGMate 账户设置中生成 Telegram 通知绑定链接，再点击链接启动机器人。"
    elif command == "/stop":
        async with request.app.state.db_pool.acquire() as conn:
            removed = await conn.fetchval(
                "SELECT unlink_telegram_user($1)",
                telegram_user_id,
            )
        reply = "TGMate 通知已关闭。" if removed else "当前没有绑定的 TGMate 通知。"
    elif command in {"/help", "/start"}:
        reply = "在 TGMate 账户设置中绑定通知，发送 /stop 可解除绑定。"

    if reply:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.platform_telegram_bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                )
        except httpx.HTTPError:
            logger.warning("Could not send Telegram bot command response")
    return {"ok": True}


@router.put("/billing/notification-preferences")
async def update_billing_notification_preferences(
    payload: BillingNotificationPreferencesRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_tenant_admin(principal)
    timezone_name = _validate_timezone(payload.timezone)
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            await conn.execute(
                """
                INSERT INTO tenant_billing_preferences (
                    tenant_id, timezone, timezone_confirmed_at,
                    email_notifications_enabled, updated_by
                )
                VALUES ($1, $2, NOW(), $3, $4)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    timezone = EXCLUDED.timezone,
                    timezone_confirmed_at = NOW(),
                    email_notifications_enabled = EXCLUDED.email_notifications_enabled,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                principal.tenant_id,
                timezone_name,
                payload.email_notifications_enabled,
                principal.user_id,
            )
            await conn.execute(
                """
                UPDATE tenant_auto_renew_settings
                SET timezone = $2, updated_at = NOW()
                WHERE tenant_id = $1
                """,
                principal.tenant_id,
                timezone_name,
            )
            if not payload.email_notifications_enabled:
                await conn.execute(
                    """
                    UPDATE billing_email_outbox
                    SET status = 'FAILED',
                        last_error_code = 'EMAIL_NOTIFICATIONS_DISABLED',
                        updated_at = NOW()
                    WHERE tenant_id = $1
                      AND status IN ('PENDING', 'PROCESSING')
                    """,
                    principal.tenant_id,
                )
    return {
        "timezone": timezone_name,
        "email_notifications_enabled": payload.email_notifications_enabled,
    }


@router.get("/billing/notifications")
async def list_billing_notifications(
    request: Request,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            rows = await conn.fetch(
                """
                SELECT id, event_type, title, body, payload, read_at, created_at
                FROM tenant_notifications
                WHERE tenant_id = $1 AND user_id = $2
                  AND ($3 = FALSE OR read_at IS NULL)
                ORDER BY created_at DESC
                LIMIT $4
                """,
                principal.tenant_id,
                principal.user_id,
                unread_only,
                limit,
            )
    return {
        "notifications": [
            {
                "id": str(row["id"]),
                "event_type": row["event_type"],
                "title": row["title"],
                "body": row["body"],
                "payload": _json_value(row["payload"]),
                "read_at": row["read_at"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.post("/billing/notifications/{notification_id}/read")
async def mark_billing_notification_read(
    notification_id: UUID,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
            row = await conn.fetchrow(
                """
                UPDATE tenant_notifications
                SET read_at = COALESCE(read_at, NOW())
                WHERE tenant_id = $1 AND user_id = $2 AND id = $3
                RETURNING id, read_at
                """,
                principal.tenant_id,
                principal.user_id,
                notification_id,
            )
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"id": str(row["id"]), "read_at": row["read_at"]}


@router.get("/admin/billing/automation-settings")
async def get_billing_automation_settings(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    async with request.app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM billing_automation_settings WHERE id = 1")
    if not row:
        raise HTTPException(status_code=503, detail="Billing automation settings are not initialized")
    result = dict(row)
    result["reminder_days"] = _json_value(result.get("reminder_days"))
    return result


@router.put("/admin/billing/automation-settings")
async def update_billing_automation_settings(
    payload: BillingAutomationSettingsRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    if payload.renewal_advance_days not in {1, 3, 7}:
        raise HTTPException(status_code=422, detail="Renewal advance must be 1, 3 or 7 days")
    if payload.notification_timezone_mode not in {"TENANT", "PLATFORM"}:
        raise HTTPException(status_code=422, detail="Timezone mode must be TENANT or PLATFORM")
    platform_timezone = _validate_timezone(payload.platform_timezone)
    if any(day < 0 or day > 365 for day in payload.reminder_days):
        raise HTTPException(status_code=422, detail="Reminder days must be between 0 and 365")
    if len(set(payload.reminder_days)) != len(payload.reminder_days):
        raise HTTPException(status_code=422, detail="Reminder days must be unique")
    reminder_days = sorted(payload.reminder_days, reverse=True)
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE billing_automation_settings
                SET renewal_advance_days = $1,
                    retry_interval_minutes = $2,
                    max_technical_retries = $3,
                    notification_timezone_mode = $4,
                    platform_timezone = $5,
                    price_change_notice_hours = $6,
                    reminder_days = $7::jsonb,
                    updated_by = $8,
                    updated_at = NOW()
                WHERE id = 1
                """,
                payload.renewal_advance_days,
                payload.retry_interval_minutes,
                payload.max_technical_retries,
                payload.notification_timezone_mode,
                platform_timezone,
                payload.price_change_notice_hours,
                json.dumps(reminder_days),
                principal.user_id,
            )
            await conn.execute(
                """
                INSERT INTO platform_audit_logs (
                    actor_user_id, action, resource_type, details
                )
                VALUES ($1, 'BILLING_AUTOMATION_SETTINGS_UPDATED',
                        'billing_automation_settings', $2::jsonb)
                """,
                principal.user_id,
                json.dumps(payload.model_dump()),
            )
    return {"status": "updated"}


async def _mark_renewal(
    pool,
    tenant_id: UUID,
    intent_id: UUID,
    *,
    status: str,
    error_code: str | None = None,
    delay_minutes: int = 0,
    order_id: UUID | None = None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute(
                """
                UPDATE subscription_renewal_intents
                SET status = $3,
                    attempt_count = CASE WHEN $4 THEN attempt_count + 1 ELSE attempt_count END,
                    last_error_code = $5,
                    next_attempt_at = NOW() + ($6 * INTERVAL '1 minute'),
                    paid_order_id = COALESCE($7, paid_order_id),
                    paid_at = CASE WHEN $3 = 'PAID' THEN NOW() ELSE paid_at END,
                    updated_at = NOW()
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                intent_id,
                status,
                error_code == "TECHNICAL_RETRY",
                error_code,
                delay_minutes,
                order_id,
            )


async def _process_renewal_intent(pool, intent) -> None:
    tenant_id = intent["tenant_id"]
    intent_id = intent["id"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            state = await conn.fetchrow(
                """
                SELECT i.*, ar.enabled AS auto_enabled,
                       ar.subscription_id AS configured_subscription_id,
                       ar.last_price_minor, ar.last_price_version,
                       s.status AS subscription_status, s.expires_at,
                       s.plan_id AS current_plan_id,
                       p.code AS current_plan_code,
                       EXISTS (
                           SELECT 1
                           FROM tenant_memberships tm
                           WHERE tm.tenant_id = i.tenant_id
                             AND tm.user_id = i.user_id
                             AND tm.role IN ('OWNER', 'ADMIN')
                       ) AS user_authorized,
                       CASE WHEN u.email_verified_at IS NOT NULL THEN u.email ELSE NULL END AS email
                FROM subscription_renewal_intents i
                LEFT JOIN tenant_auto_renew_settings ar
                  ON ar.tenant_id = i.tenant_id
                LEFT JOIN tenant_subscriptions s
                  ON s.tenant_id = i.tenant_id AND s.id = i.subscription_id
                LEFT JOIN membership_plans p ON p.id = s.plan_id
                LEFT JOIN users u ON u.id = i.user_id
                WHERE i.tenant_id = $1 AND i.id = $2
                """,
                tenant_id,
                intent_id,
            )
            if not state or state["status"] != "PROCESSING":
                return
            if (
                not state["auto_enabled"]
                or not state["user_authorized"]
                or state["configured_subscription_id"] != state["subscription_id"]
            ):
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET status = 'CANCELED', last_error_code = 'AUTO_RENEW_DISABLED',
                        updated_at = NOW()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    intent_id,
                )
                await conn.execute(
                    """
                    UPDATE tenant_auto_renew_settings
                    SET enabled = FALSE, updated_at = NOW()
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                )
                return
            if (
                state["subscription_status"] not in {"ACTIVE", "TRIALING"}
                or state["current_plan_id"] != state["plan_id"]
                or state["current_plan_code"] != state["plan_code_snapshot"]
                or state["expires_at"] is None
                or state["expires_at"] <= await conn.fetchval("SELECT NOW()")
            ):
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET status = 'EXPIRED', last_error_code = 'SUBSCRIPTION_EXPIRED',
                        updated_at = NOW()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    intent_id,
                )
                await conn.execute(
                    """
                    UPDATE tenant_auto_renew_settings
                    SET enabled = FALSE, updated_at = NOW()
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                )
                return

            current_price = await conn.fetchrow(
                """
                SELECT amount_minor, version
                FROM membership_plan_prices
                WHERE plan_id = $1 AND currency = $2
                  AND duration_days = $3 AND is_current = TRUE
                """,
                state["plan_id"],
                state["currency"],
                state["duration_days"],
            )
            if not current_price:
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET status = 'FAILED', last_error_code = 'CURRENT_PRICE_MISSING',
                        updated_at = NOW()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    intent_id,
                )
                return

            price_minor = int(current_price["amount_minor"])
            price_version = int(current_price["version"])
            intent_amount_changed = int(state["amount_minor"]) != price_minor
            baseline_amount_changed = (
                state["last_price_minor"] is not None
                and int(state["last_price_minor"]) != price_minor
            )
            price_amount_changed = intent_amount_changed or baseline_amount_changed
            price_version_changed = (
                int(state["price_version"]) != price_version
                or (
                    state["last_price_version"] is not None
                    and int(state["last_price_version"]) != price_version
                )
            )
            if price_amount_changed:
                old_price = (
                    int(state["last_price_minor"])
                    if baseline_amount_changed
                    else int(state["amount_minor"])
                )
                notice_hours = int(await conn.fetchval(
                    "SELECT price_change_notice_hours FROM billing_automation_settings WHERE id = 1"
                ))
                body = (
                    f"自动续费价格发生变化：{_format_amount(state['currency'], old_price)} "
                    f"调整为 {_format_amount(state['currency'], price_minor)}。"
                    f"系统将在至少 {notice_hours} 小时通知等待期结束后，按最新价格尝试从钱包余额扣款。"
                    "你可以在等待期间关闭自动续费。"
                )
                email_enabled = await _tenant_email_enabled(conn, tenant_id)
                await _notify_user(
                    conn,
                    tenant_id=tenant_id,
                    user_id=state["user_id"],
                    dedupe_key=f"renewal-price:{state['renewal_cycle_key']}:{price_version}",
                    event_type="AUTO_RENEW_PRICE_CHANGED",
                    title="会员自动续费价格调整通知",
                    body=body,
                    payload={
                        "currency": state["currency"],
                        "old_amount_minor": old_price,
                        "new_amount_minor": price_minor,
                        "price_version": price_version,
                        "notice_hours": notice_hours,
                    },
                    email_enabled=email_enabled,
                    recipient_email=state["email"],
                )
                fingerprint = hashlib.sha256(
                    f"{state['plan_code_snapshot']}|{state['duration_days']}|{state['currency']}|{price_minor}|{price_version}".encode()
                ).hexdigest()
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET amount_minor = $3, price_version = $4,
                        request_fingerprint = $5, status = 'PENDING',
                        price_notice_sent_at = NOW(),
                        next_attempt_at = NOW() + ($6 * INTERVAL '1 hour'),
                        updated_at = NOW()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    intent_id,
                    price_minor,
                    price_version,
                    fingerprint,
                    notice_hours,
                )
                await conn.execute(
                    """
                    UPDATE tenant_auto_renew_settings
                    SET last_price_minor = $2, last_price_version = $3,
                        updated_at = NOW()
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                    price_minor,
                    price_version,
                )
                return
            if price_version_changed:
                fingerprint = hashlib.sha256(
                    f"{state['plan_code_snapshot']}|{state['duration_days']}|{state['currency']}|{price_minor}|{price_version}".encode()
                ).hexdigest()
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET amount_minor = $3, price_version = $4,
                        request_fingerprint = $5, updated_at = NOW()
                    WHERE tenant_id = $1 AND id = $2
                    """,
                    tenant_id,
                    intent_id,
                    price_minor,
                    price_version,
                    fingerprint,
                )
                await conn.execute(
                    """
                    UPDATE tenant_auto_renew_settings
                    SET last_price_minor = $2, last_price_version = $3,
                        updated_at = NOW()
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                    price_minor,
                    price_version,
                )

            # The commerce transaction checks this exact price version again before charging.
            expected_version = price_version
            expected_amount = price_minor
            plan_code = state["plan_code_snapshot"]
            duration_days = int(state["duration_days"])
            currency = state["currency"]
            cycle_key = state["renewal_cycle_key"]
            user_id = state["user_id"]

    fake_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    )
    fake_principal = AuthPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        family_id=uuid4(),
        role="OWNER",
        email="",
        platform_admin=False,
    )
    purchase_payload = PurchaseRequest(
        plan_code=plan_code,
        duration_days=duration_days,
        currency=currency,
        idempotency_key=f"auto-renew:{cycle_key}",
        expected_price_version=expected_version,
        expected_amount_minor=expected_amount,
    )
    try:
        result = await purchase_membership(purchase_payload, fake_request, fake_principal)
    except HTTPException as exc:
        if exc.status_code == 402:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                    retry_minutes = int(await conn.fetchval(
                        "SELECT retry_interval_minutes FROM billing_automation_settings WHERE id = 1"
                    ))
                    email_enabled = await _tenant_email_enabled(conn, tenant_id)
                    balance_body = (
                        f"会员自动续费所需余额不足。需要 {_format_amount(currency, int(detail.get('required_minor', 0)))}，"
                        f"当前可用 {_format_amount(currency, int(detail.get('available_minor', 0)))}。"
                        "请在会员到期前充值；如果会员已经到期，系统不会再自动扣款，需要你手动选择套餐。"
                    )
                    await _notify_user(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        dedupe_key=f"renewal-balance:{cycle_key}",
                        event_type="AUTO_RENEW_INSUFFICIENT_BALANCE",
                        title="会员自动续费余额不足",
                        body=balance_body,
                        payload=detail,
                        email_enabled=email_enabled,
                        recipient_email=state["email"],
                    )
                    await conn.execute(
                        """
                        UPDATE subscription_renewal_intents
                        SET status = 'WAITING_FOR_BALANCE',
                            next_attempt_at = NOW() + ($3 * INTERVAL '1 minute'),
                            last_error_code = 'INSUFFICIENT_BALANCE',
                            updated_at = NOW()
                        WHERE tenant_id = $1 AND id = $2
                        """,
                        tenant_id,
                        intent_id,
                        retry_minutes,
                    )
            return
        if exc.status_code == 409 and exc.detail in {
            "AUTO_RENEW_DISABLED",
            "AUTO_RENEW_INTENT_NOT_ACTIVE",
        }:
            await _mark_renewal(pool, tenant_id, intent_id, status="CANCELED", error_code=str(exc.detail))
            return
        if exc.status_code == 409 and exc.detail == "AUTO_RENEW_PRICE_CHANGED":
            await _mark_renewal(pool, tenant_id, intent_id, status="RETRY", error_code="PRICE_CHANGED_RECHECK", delay_minutes=0)
            return
        await _record_renewal_failure(pool, state, exc.__class__.__name__)
        return
    except Exception as exc:
        logger.exception("Automatic renewal failed for intent %s", intent_id)
        await _record_renewal_failure(pool, state, exc.__class__.__name__)
        return

    order_id = UUID(result["order_id"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute(
                """
                UPDATE subscription_renewal_intents
                SET status = 'PAID', paid_order_id = $3, paid_at = NOW(),
                    last_error_code = NULL, updated_at = NOW()
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                intent_id,
                order_id,
            )
            await conn.execute(
                """
                UPDATE tenant_auto_renew_settings
                SET last_price_minor = $2, last_price_version = $3,
                    updated_at = NOW()
                WHERE tenant_id = $1
                """,
                tenant_id,
                expected_amount,
                expected_version,
            )
            email_enabled = await _tenant_email_enabled(conn, tenant_id)
            await _notify_user(
                conn,
                tenant_id=tenant_id,
                user_id=user_id,
                dedupe_key=f"renewal-paid:{cycle_key}",
                event_type="AUTO_RENEW_PAID",
                title="会员自动续费成功",
                body=f"已成功从钱包扣除 {_format_amount(currency, expected_amount)}，会员期限已顺延。",
                payload={"order_id": str(order_id), "currency": currency, "amount_minor": expected_amount},
                email_enabled=email_enabled,
                recipient_email=state["email"],
            )


async def _record_renewal_failure(pool, state, error_code: str) -> None:
    tenant_id = state["tenant_id"]
    intent_id = state["id"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            retry = await conn.fetchrow(
                """
                SELECT i.attempt_count, i.max_attempts,
                       cfg.retry_interval_minutes
                FROM subscription_renewal_intents i
                CROSS JOIN billing_automation_settings cfg
                WHERE i.tenant_id = $1 AND i.id = $2
                """,
                tenant_id,
                intent_id,
            )
            if not retry:
                return
            attempts = int(retry["attempt_count"]) + 1
            final = attempts >= int(retry["max_attempts"])
            status = "FAILED" if final else "RETRY"
            await conn.execute(
                """
                UPDATE subscription_renewal_intents
                SET status = $3, attempt_count = $4,
                    last_error_code = $5,
                    next_attempt_at = NOW() + ($6 * INTERVAL '1 minute'),
                    updated_at = NOW()
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                intent_id,
                status,
                attempts,
                error_code[:80],
                int(retry["retry_interval_minutes"]),
            )
            if final:
                email_enabled = await _tenant_email_enabled(conn, tenant_id)
                await _notify_user(
                    conn,
                    tenant_id=tenant_id,
                    user_id=state["user_id"],
                    dedupe_key=f"renewal-failed:{state['renewal_cycle_key']}",
                    event_type="AUTO_RENEW_FAILED",
                    title="会员自动续费未能完成",
                    body="系统已达到自动重试次数上限，请检查钱包或支付状态并手动续费。",
                    payload={"error_code": error_code},
                    email_enabled=email_enabled,
                    recipient_email=state["email"],
                )


async def _process_expiry_reminders(pool) -> None:
    async with pool.acquire() as conn:
        config = await conn.fetchrow("SELECT reminder_days FROM billing_automation_settings WHERE id = 1")
        rows = await conn.fetch("SELECT * FROM list_billing_reminder_recipients(5000)")
        now = await conn.fetchval("SELECT NOW()")
    configured_days = _json_value(config["reminder_days"]) if config else [7, 3, 1, 0]
    reminder_days = [int(value) for value in configured_days]
    grouped: dict[tuple[UUID, UUID], dict] = {}
    for row in rows:
        key = (row["tenant_id"], row["subscription_id"])
        grouped.setdefault(key, {"row": row, "recipients": []})["recipients"].append(row)

    for (tenant_id, subscription_id), group in grouped.items():
        row = group["row"]
        expires_at = row["expires_at"]
        timezone_name = row["timezone"] or "UTC"
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
            timezone_name = "UTC"
        local_expiry = expires_at.astimezone(zone)
        expired = expires_at <= now or row["subscription_status"] == "EXPIRED"
        bucket = None if expired else reminder_bucket(expires_at, now, reminder_days)
        if not expired and bucket is None:
            continue

        async with pool.acquire() as tenant_conn:
            async with tenant_conn.transaction():
                await tenant_conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                current = await tenant_conn.fetchrow(
                    """
                    SELECT status, expiry_notified_at
                    FROM tenant_subscriptions
                    WHERE tenant_id = $1 AND id = $2
                    FOR UPDATE
                    """,
                    tenant_id,
                    subscription_id,
                )
                if not current:
                    continue
                if expired and current["expiry_notified_at"] is not None:
                    continue
                if expired:
                    await tenant_conn.execute(
                        """
                        UPDATE tenant_subscriptions
                        SET status = 'EXPIRED',
                            expiry_notified_at = COALESCE(expiry_notified_at, NOW()),
                            updated_at = NOW()
                        WHERE tenant_id = $1 AND id = $2
                        """,
                        tenant_id,
                        subscription_id,
                    )
                    await tenant_conn.execute(
                        """
                        UPDATE tenant_auto_renew_settings
                        SET enabled = FALSE, updated_at = NOW()
                        WHERE tenant_id = $1 AND subscription_id = $2
                        """,
                        tenant_id,
                        subscription_id,
                    )
                    await tenant_conn.execute(
                        """
                        UPDATE subscription_renewal_intents
                        SET status = 'EXPIRED',
                            last_error_code = 'SUBSCRIPTION_EXPIRED',
                            updated_at = NOW()
                        WHERE tenant_id = $1 AND subscription_id = $2
                          AND status IN ('PENDING', 'PROCESSING', 'WAITING_FOR_BALANCE', 'RETRY', 'FAILED')
                        """,
                        tenant_id,
                        subscription_id,
                    )
                    event_type = "SUBSCRIPTION_EXPIRED"
                    title = "会员服务已到期"
                    body = (
                        f"{row['plan_name']} 已于 {local_expiry:%Y-%m-%d %H:%M} "
                        f"({timezone_name}) 到期。付费权益已停止；历史数据仍予保留。"
                        "续费后可重新选择需要恢复的资源。"
                    )
                    dedupe_suffix = "expired"
                else:
                    event_type = "SUBSCRIPTION_EXPIRING"
                    title = f"会员将在 {bucket} 天后到期" if bucket else "会员即将到期"
                    body = (
                        f"{row['plan_name']} 将于 {local_expiry:%Y-%m-%d %H:%M} "
                        f"({timezone_name}) 到期，请及时续费。"
                    )
                    dedupe_suffix = f"reminder-{bucket}d"

                email_enabled = await _tenant_email_enabled(tenant_conn, tenant_id)
                expiry_key = expires_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                for recipient in group["recipients"]:
                    await _notify_user(
                        tenant_conn,
                        tenant_id=tenant_id,
                        user_id=recipient["user_id"],
                        dedupe_key=f"subscription:{subscription_id}:{expiry_key}:{dedupe_suffix}:user:{recipient['user_id']}",
                        event_type=event_type,
                        title=title,
                        body=body,
                        payload={
                            "subscription_id": str(subscription_id),
                            "plan_code": row["plan_code"],
                            "expires_at": expires_at.isoformat(),
                            "timezone": timezone_name,
                            "reminder_days": bucket,
                        },
                        email_enabled=email_enabled and recipient["email_notifications_enabled"],
                        recipient_email=recipient["email"],
                    )


async def _send_due_billing_emails(pool, lock_conn) -> None:
    rows = await lock_conn.fetch("SELECT * FROM claim_due_billing_emails(50)")
    for row in rows:
        tenant_id = row["tenant_id"]
        email = row["email"]
        if not email:
            status = "FAILED"
            error_code = "RECIPIENT_EMAIL_UNVERIFIED"
        else:
            async with pool.acquire() as preference_conn:
                async with preference_conn.transaction():
                    await preference_conn.execute(
                        "SELECT set_config('app.tenant_id', $1, true)",
                        str(tenant_id),
                    )
                    email_enabled = await _tenant_email_enabled(preference_conn, tenant_id)
            if not email_enabled:
                status = "FAILED"
                error_code = "EMAIL_NOTIFICATIONS_DISABLED"
            else:
                try:
                    await send_billing_notification_email(
                        email,
                        row["subject"],
                        row["body"],
                        message_id=f"<billing-{row['id']}@tgmate.com>",
                    )
                    status = "SENT"
                    error_code = None
                except Exception as exc:
                    logger.warning("Billing email delivery failed id=%s error=%s", row["id"], type(exc).__name__)
                    status = "FAILED" if int(row["attempt_count"]) >= 5 else "PENDING"
                    error_code = type(exc).__name__[:80]

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                if status == "SENT":
                    await conn.execute(
                        """
                        UPDATE billing_email_outbox
                        SET status = 'SENT', sent_at = NOW(),
                            last_error_code = NULL, updated_at = NOW()
                        WHERE tenant_id = $1 AND id = $2
                        """,
                        tenant_id,
                        row["id"],
                    )
                else:
                    delay_seconds = min(21600, 60 * (2 ** min(int(row["attempt_count"]), 8)))
                    await conn.execute(
                        """
                        UPDATE billing_email_outbox
                        SET status = $3, last_error_code = $4,
                            next_attempt_at = NOW() + ($5 * INTERVAL '1 second'),
                            updated_at = NOW()
                        WHERE tenant_id = $1 AND id = $2
                        """,
                        tenant_id,
                        row["id"],
                        status,
                        error_code,
                        delay_seconds,
                    )


async def _wait_or_stop(stop_event: asyncio.Event, seconds: int) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_billing_automation(pool, stop_event: asyncio.Event) -> None:
    """Run one database-leased renewal, reminder and email worker across API replicas."""
    while not stop_event.is_set():
        try:
            async with pool.acquire() as lock_conn:
                acquired = await lock_conn.fetchval(
                    "SELECT pg_try_advisory_lock($1)",
                    _BILLING_AUTOMATION_LOCK_ID,
                )
                if not acquired:
                    await _wait_or_stop(stop_event, 30)
                    continue
                logger.info("Billing automation worker acquired its database lease")
                try:
                    while not stop_event.is_set():
                        try:
                            await lock_conn.fetchval("SELECT create_due_auto_renew_intents(100)")
                            intents = await lock_conn.fetch("SELECT * FROM claim_due_auto_renew_intents(50)")
                            for intent in intents:
                                if stop_event.is_set():
                                    break
                                await _process_renewal_intent(pool, intent)
                            await _process_expiry_reminders(pool)
                            await _send_due_billing_emails(pool, lock_conn)
                        except Exception:
                            logger.exception("Billing automation iteration failed")
                            if lock_conn.is_closed():
                                break
                        await _wait_or_stop(stop_event, 60)
                finally:
                    try:
                        await lock_conn.fetchval(
                            "SELECT pg_advisory_unlock($1)",
                            _BILLING_AUTOMATION_LOCK_ID,
                        )
                    except Exception:
                        logger.warning("Billing automation database lease could not be released cleanly")
        except Exception:
            logger.exception("Billing automation worker lost its database connection")
            await _wait_or_stop(stop_event, 30)
