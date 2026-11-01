from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthPrincipal, get_current_principal


router = APIRouter(tags=["wallet and purchases"])


class PurchaseQuoteRequest(BaseModel):
    plan_code: str = Field(min_length=2, max_length=40)
    duration_days: int = Field(ge=1, le=3650)
    currency: str = Field(min_length=3, max_length=8)


class PurchaseRequest(PurchaseQuoteRequest):
    idempotency_key: str = Field(min_length=16, max_length=120)
    expected_price_version: int | None = Field(default=None, ge=1)
    expected_amount_minor: int | None = Field(default=None, ge=0)


class PlanPriceRequest(BaseModel):
    amount_minor: int = Field(ge=0, le=9_000_000_000_000_000)


class WalletCreditRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=8)
    amount_minor: int = Field(gt=0, le=9_000_000_000_000_000)
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=120)


def _normalize_currency(currency: str) -> str:
    normalized = currency.strip().upper()
    if normalized not in {"CNY", "USD", "USDT"}:
        raise HTTPException(
            status_code=422,
            detail="Supported currencies are CNY, USD and USDT",
        )
    return normalized


def purchase_fingerprint(plan_code: str, duration_days: int, currency: str) -> str:
    canonical = f"{plan_code.strip().upper()}|{duration_days}|{currency.strip().upper()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        (value.days * 86400 + value.seconds) * 1_000_000
        + value.microseconds
    )


def calculate_remaining_credit(
    terms: Sequence[Mapping[str, Any]],
    target_currency: str,
    now: datetime,
) -> int:
    """Return the unconsumed paid value, rounded down in integer minor units."""
    target_currency = target_currency.upper()
    total = 0
    for term in terms:
        net_paid = int(term["net_paid_minor"])
        starts_at = term["starts_at"]
        ends_at = term["ends_at"]
        if ends_at <= now or net_paid <= 0:
            continue
        duration_us = _timedelta_microseconds(ends_at - starts_at)
        if duration_us <= 0:
            continue
        remaining_from = max(now, starts_at)
        remaining_us = _timedelta_microseconds(ends_at - remaining_from)
        if remaining_us <= 0:
            continue
        if str(term["currency"]).upper() != target_currency:
            raise ValueError("Remaining membership value uses a different currency")
        total += (net_paid * remaining_us) // duration_us
    return total


async def _load_purchase_quote(
    conn,
    tenant_id: UUID,
    plan_code: str,
    duration_days: int,
    currency: str,
    *,
    lock_current: bool = False,
) -> dict[str, Any]:
    currency = _normalize_currency(currency)
    plan = await conn.fetchrow(
        """
        SELECT p.id AS plan_id, p.code, p.name, p.level,
               pr.amount_minor, pr.version AS price_version
        FROM membership_plans p
        JOIN membership_plan_prices pr ON pr.plan_id = p.id
        WHERE p.code = $1
          AND p.is_active = TRUE
          AND pr.currency = $2
          AND pr.duration_days = $3
          AND pr.is_current = TRUE
        """,
        plan_code.strip().upper(),
        currency,
        duration_days,
    )
    if not plan:
        raise HTTPException(
            status_code=404,
            detail="No active price is configured for this plan, duration and currency",
        )

    lock_clause = "FOR UPDATE OF s" if lock_current else ""
    current = await conn.fetchrow(
        f"""
        SELECT s.id, s.plan_id, s.status, s.starts_at, s.expires_at,
               p.code AS current_plan_code, p.name AS current_plan_name,
               p.level AS current_plan_level
        FROM tenant_subscriptions s
        JOIN membership_plans p ON p.id = s.plan_id
        WHERE s.tenant_id = $1
          AND s.starts_at <= NOW()
          AND s.status IN ('ACTIVE', 'TRIALING')
          AND (s.expires_at IS NULL OR s.expires_at > NOW())
        ORDER BY s.starts_at DESC
        LIMIT 1
        {lock_clause}
        """,
        tenant_id,
    )
    db_now = await conn.fetchval("SELECT NOW()")
    current_level = int(current["current_plan_level"]) if current else 0
    target_level = int(plan["level"])
    if current and target_level < current_level:
        raise HTTPException(
            status_code=409,
            detail="Downgrades are unavailable while the current subscription is active",
        )

    if current and plan["code"] == current["current_plan_code"]:
        if current["expires_at"] is None:
            raise HTTPException(
                status_code=409,
                detail="This subscription has no expiry date and does not need renewal",
            )
        existing_term_currencies = await conn.fetch(
            """
            SELECT DISTINCT currency
            FROM subscription_terms
            WHERE tenant_id = $1
              AND status = 'ACTIVE'
              AND ends_at > $2
              AND net_paid_minor > 0
            """,
            tenant_id,
            db_now,
        )
        if any(row["currency"] != currency for row in existing_term_currencies):
            raise HTTPException(
                status_code=409,
                detail="Renewal must use the same currency as the remaining paid membership value",
            )
        order_kind = "RENEWAL"
        effective_start = max(current["expires_at"], db_now)
        upgrade_credit = 0
    elif current and target_level > current_level:
        order_kind = "UPGRADE"
        effective_start = db_now
        terms = await conn.fetch(
            """
            SELECT currency, net_paid_minor, starts_at, ends_at
            FROM subscription_terms
            WHERE tenant_id = $1
              AND status = 'ACTIVE'
              AND ends_at > $2
            ORDER BY starts_at
            """,
            tenant_id,
            db_now,
        )
        try:
            upgrade_credit = calculate_remaining_credit(terms, currency, db_now)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="Upgrade must use the same currency as the remaining paid membership value",
            ) from exc
    else:
        order_kind = "PURCHASE"
        effective_start = db_now
        upgrade_credit = 0

    list_amount = int(plan["amount_minor"])
    upgrade_credit = min(upgrade_credit, list_amount)
    payable = max(0, list_amount - upgrade_credit)
    wallet_balance = await conn.fetchval(
        """
        SELECT balance_minor
        FROM tenant_wallet_accounts
        WHERE tenant_id = $1 AND currency = $2
        """,
        tenant_id,
        currency,
    )
    wallet_balance = int(wallet_balance or 0)
    effective_end = effective_start + timedelta(days=duration_days)
    return {
        "plan_id": plan["plan_id"],
        "plan_code": plan["code"],
        "plan_name": plan["name"],
        "plan_level": target_level,
        "order_kind": order_kind,
        "duration_days": duration_days,
        "currency": currency,
        "list_amount_minor": list_amount,
        "upgrade_credit_minor": upgrade_credit,
        "payable_amount_minor": payable,
        "price_version": int(plan["price_version"]),
        "wallet_balance_minor": wallet_balance,
        "wallet_sufficient": wallet_balance >= payable,
        "effective_start_at": effective_start,
        "effective_end_at": effective_end,
        "current_subscription": current,
        "db_now": db_now,
    }


def _order_response(row, balance_minor: int | None = None) -> dict[str, Any]:
    result = {
        "order_id": str(row["id"]),
        "order_no": row["order_no"],
        "order_kind": row["order_kind"],
        "plan_code": row["plan_code_snapshot"],
        "plan_name": row["plan_name_snapshot"],
        "duration_days": row["duration_days"],
        "currency": row["currency"],
        "list_amount_minor": row["list_amount_minor"],
        "upgrade_credit_minor": row["upgrade_credit_minor"],
        "payable_amount_minor": row["payable_amount_minor"],
        "status": row["status"],
        "paid_at": row["paid_at"],
        "effective_start_at": row["effective_start_at"],
        "effective_end_at": row["effective_end_at"],
    }
    if balance_minor is not None:
        result["wallet_balance_minor"] = balance_minor
    return result


@router.get("/billing/prices")
async def list_current_prices(request: Request):
    # Price rows are global catalogue data; no tenant-specific fields are exposed.
    # The caller may browse prices before signing in.
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.code AS plan_code, p.name AS plan_name, p.level,
                   pr.currency, pr.duration_days, pr.amount_minor,
                   pr.version, pr.created_at
            FROM membership_plan_prices pr
            JOIN membership_plans p ON p.id = pr.plan_id
            WHERE p.is_active = TRUE
              AND pr.is_current = TRUE
            ORDER BY p.level, pr.currency, pr.duration_days
            """
        )
    return {
        "prices": [
            {
                "plan_code": row["plan_code"],
                "plan_name": row["plan_name"],
                "level": row["level"],
                "currency": row["currency"],
                "duration_days": row["duration_days"],
                "amount_minor": row["amount_minor"],
                "version": row["version"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.get("/billing/wallet")
async def get_wallet(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            rows = await conn.fetch(
                """
                SELECT currency, balance_minor, updated_at
                FROM tenant_wallet_accounts
                WHERE tenant_id = $1
                ORDER BY currency
                """,
                principal.tenant_id,
            )
    return {
        "balances": [
            {
                "currency": row["currency"],
                "balance_minor": row["balance_minor"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    }


@router.get("/billing/wallet/ledger")
async def get_wallet_ledger(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            rows = await conn.fetch(
                """
                SELECT id, currency, direction, entry_type, amount_minor,
                       balance_after_minor, reference_type, reference_id,
                       reason, created_at
                FROM wallet_ledger_entries
                WHERE tenant_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2
                """,
                principal.tenant_id,
                limit,
            )
    return {
        "entries": [
            {
                "id": str(row["id"]),
                "currency": row["currency"],
                "direction": row["direction"],
                "entry_type": row["entry_type"],
                "amount_minor": row["amount_minor"],
                "balance_after_minor": row["balance_after_minor"],
                "reference_type": row["reference_type"],
                "reference_id": str(row["reference_id"]) if row["reference_id"] else None,
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.get("/billing/orders")
async def list_billing_orders(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            rows = await conn.fetch(
                """
                SELECT id, order_no, order_kind, plan_code_snapshot,
                       plan_name_snapshot, duration_days, currency,
                       list_amount_minor, upgrade_credit_minor,
                       payable_amount_minor, status, paid_at,
                       effective_start_at, effective_end_at
                FROM billing_orders
                WHERE tenant_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                principal.tenant_id,
                limit,
            )
    return {"orders": [_order_response(row) for row in rows]}


@router.post("/billing/purchases/quote")
async def quote_membership_purchase(
    payload: PurchaseQuoteRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    currency = _normalize_currency(payload.currency)
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            quote = await _load_purchase_quote(
                conn,
                principal.tenant_id,
                payload.plan_code,
                payload.duration_days,
                currency,
            )
    return {
        key: value
        for key, value in quote.items()
        if key not in {"current_subscription", "db_now", "plan_id", "plan_level", "price_version"}
    } | {
        "price_version": quote["price_version"],
        "current_plan_code": (
            quote["current_subscription"]["current_plan_code"]
            if quote["current_subscription"]
            else "FREE"
        ),
    }


@router.post("/billing/purchases")
async def purchase_membership(
    payload: PurchaseRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    currency = _normalize_currency(payload.currency)
    plan_code = payload.plan_code.strip().upper()
    fingerprint = purchase_fingerprint(plan_code, payload.duration_days, currency)

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            tenant = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                principal.tenant_id,
            )
            if not tenant:
                raise HTTPException(status_code=404, detail="Workspace not found")

            existing = await conn.fetchrow(
                """
                SELECT * FROM billing_orders
                WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                principal.tenant_id,
                payload.idempotency_key,
            )
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different purchase",
                    )
                balance = await conn.fetchval(
                    """
                    SELECT balance_minor FROM tenant_wallet_accounts
                    WHERE tenant_id = $1 AND currency = $2
                    """,
                    principal.tenant_id,
                    currency,
                )
                return _order_response(existing, int(balance or 0))

            quote = await _load_purchase_quote(
                conn,
                principal.tenant_id,
                plan_code,
                payload.duration_days,
                currency,
                lock_current=True,
            )
            if (
                payload.expected_price_version is not None
                and (
                    payload.expected_price_version != quote["price_version"]
                    or payload.expected_amount_minor != quote["list_amount_minor"]
                )
            ):
                raise HTTPException(status_code=409, detail="AUTO_RENEW_PRICE_CHANGED")

            if payload.idempotency_key.startswith("auto-renew:"):
                current_subscription = quote["current_subscription"]
                auto_renew = await conn.fetchrow(
                    """
                    SELECT enabled, subscription_id
                    FROM tenant_auto_renew_settings
                    WHERE tenant_id = $1
                    FOR UPDATE
                    """,
                    principal.tenant_id,
                )
                if (
                    not current_subscription
                    or not auto_renew
                    or not auto_renew["enabled"]
                    or auto_renew["subscription_id"] != current_subscription["id"]
                ):
                    raise HTTPException(status_code=409, detail="AUTO_RENEW_DISABLED")
                active_intent = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM subscription_renewal_intents
                        WHERE tenant_id = $1
                          AND idempotency_key = $2
                          AND status = 'PROCESSING'
                    )
                    """,
                    principal.tenant_id,
                    payload.idempotency_key,
                )
                if not active_intent:
                    raise HTTPException(status_code=409, detail="AUTO_RENEW_INTENT_NOT_ACTIVE")

            await conn.execute(
                """
                INSERT INTO tenant_wallet_accounts (tenant_id, currency)
                VALUES ($1, $2)
                ON CONFLICT (tenant_id, currency) DO NOTHING
                """,
                principal.tenant_id,
                currency,
            )
            wallet = await conn.fetchrow(
                """
                SELECT balance_minor FROM tenant_wallet_accounts
                WHERE tenant_id = $1 AND currency = $2
                FOR UPDATE
                """,
                principal.tenant_id,
                currency,
            )
            balance_before = int(wallet["balance_minor"])
            payable = quote["payable_amount_minor"]
            if balance_before < payable:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "message": "Insufficient wallet balance",
                        "currency": currency,
                        "required_minor": payable,
                        "available_minor": balance_before,
                    },
                )

            order_id = uuid4()
            order_no = f"TGS-{order_id.hex[:24].upper()}"
            db_now = quote["db_now"]
            effective_start = quote["effective_start_at"]
            effective_end = quote["effective_end_at"]
            order = await conn.fetchrow(
                """
                INSERT INTO billing_orders (
                    id, tenant_id, order_no, order_kind, plan_id,
                    plan_code_snapshot, plan_name_snapshot, duration_days,
                    currency, list_amount_minor, upgrade_credit_minor,
                    payable_amount_minor, price_version, status,
                    payment_source, idempotency_key, request_fingerprint,
                    created_by, paid_at, effective_start_at, effective_end_at,
                    metadata
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12, $13, 'PAID', 'WALLET', $14, $15, $16, $17,
                    $18, $19, $20::jsonb
                )
                RETURNING *
                """,
                order_id,
                principal.tenant_id,
                order_no,
                quote["order_kind"],
                quote["plan_id"],
                quote["plan_code"],
                quote["plan_name"],
                payload.duration_days,
                currency,
                quote["list_amount_minor"],
                quote["upgrade_credit_minor"],
                payable,
                quote["price_version"],
                payload.idempotency_key,
                fingerprint,
                principal.user_id,
                db_now,
                effective_start,
                effective_end,
                json.dumps({
                    "wallet_balance_before_minor": balance_before,
                    "current_plan_code": (
                        quote["current_subscription"]["current_plan_code"]
                        if quote["current_subscription"]
                        else "FREE"
                    ),
                }),
            )

            if payable > 0:
                balance_after = balance_before - payable
                await conn.execute(
                    """
                    UPDATE tenant_wallet_accounts
                    SET balance_minor = $3, updated_at = NOW()
                    WHERE tenant_id = $1 AND currency = $2
                    """,
                    principal.tenant_id,
                    currency,
                    balance_after,
                )
                await conn.execute(
                    """
                    INSERT INTO wallet_ledger_entries (
                        tenant_id, currency, direction, entry_type,
                        amount_minor, balance_after_minor, reference_type,
                        reference_id, idempotency_key, actor_user_id, reason
                    )
                    VALUES ($1, $2, 'DEBIT', 'PURCHASE_DEBIT', $3, $4,
                            'BILLING_ORDER', $5, $6, $7, $8)
                    """,
                    principal.tenant_id,
                    currency,
                    payable,
                    balance_after,
                    order_id,
                    f"billing-order:{order_id}",
                    principal.user_id,
                    f"Membership {quote['plan_code']} purchase",
                )
            else:
                balance_after = balance_before

            current = quote["current_subscription"]
            if current and quote["order_kind"] == "UPGRADE":
                await conn.execute(
                    """
                    UPDATE subscription_terms
                    SET status = 'SUPERSEDED'
                    WHERE tenant_id = $1
                      AND status = 'ACTIVE'
                      AND ends_at > $2
                    """,
                    principal.tenant_id,
                    db_now,
                )
                await conn.execute(
                    """
                    UPDATE tenant_subscriptions
                    SET plan_id = $2, status = 'ACTIVE', starts_at = $3,
                        expires_at = $4, billing_source = 'WALLET',
                        updated_at = NOW()
                    WHERE id = $1 AND tenant_id = $5
                    """,
                    current["id"],
                    quote["plan_id"],
                    effective_start,
                    effective_end,
                    principal.tenant_id,
                )
            elif current and quote["order_kind"] == "RENEWAL":
                await conn.execute(
                    """
                    UPDATE tenant_subscriptions
                    SET expires_at = $2, status = 'ACTIVE',
                        billing_source = 'WALLET', updated_at = NOW()
                    WHERE id = $1 AND tenant_id = $3
                    """,
                    current["id"],
                    effective_end,
                    principal.tenant_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE tenant_subscriptions
                    SET status = 'EXPIRED', updated_at = NOW()
                    WHERE tenant_id = $1
                      AND status IN ('ACTIVE', 'TRIALING', 'PAST_DUE')
                      AND expires_at IS NOT NULL
                      AND expires_at <= $2
                    """,
                    principal.tenant_id,
                    db_now,
                )
                await conn.execute(
                    """
                    UPDATE tenant_subscriptions
                    SET status = 'EXPIRED', updated_at = NOW()
                    WHERE tenant_id = $1 AND status = 'PAST_DUE'
                    """,
                    principal.tenant_id,
                )
                await conn.execute(
                    """
                    INSERT INTO tenant_subscriptions (
                        tenant_id, plan_id, status, starts_at, expires_at,
                        billing_source
                    )
                    VALUES ($1, $2, 'ACTIVE', $3, $4, 'WALLET')
                    """,
                    principal.tenant_id,
                    quote["plan_id"],
                    effective_start,
                    effective_end,
                )

            is_auto_renew_purchase = payload.idempotency_key.startswith("auto-renew:")
            if not is_auto_renew_purchase:
                if quote["order_kind"] != "RENEWAL":
                    await conn.execute(
                        """
                        UPDATE tenant_auto_renew_settings
                        SET enabled = FALSE, updated_at = NOW()
                        WHERE tenant_id = $1
                        """,
                        principal.tenant_id,
                    )
                await conn.execute(
                    """
                    UPDATE subscription_renewal_intents
                    SET status = 'CANCELED',
                        last_error_code = 'MANUAL_SUBSCRIPTION_CHANGE',
                        updated_at = NOW()
                    WHERE tenant_id = $1
                      AND status IN ('PENDING', 'PROCESSING', 'WAITING_FOR_BALANCE', 'RETRY', 'FAILED')
                    """,
                    principal.tenant_id,
                )

            await conn.execute(
                """
                INSERT INTO subscription_terms (
                    tenant_id, order_id, plan_id, plan_code_snapshot,
                    currency, duration_days, gross_amount_minor,
                    upgrade_credit_minor, net_paid_minor, starts_at, ends_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                principal.tenant_id,
                order_id,
                quote["plan_id"],
                quote["plan_code"],
                currency,
                payload.duration_days,
                quote["list_amount_minor"],
                quote["upgrade_credit_minor"],
                payable,
                effective_start,
                effective_end,
            )
            await conn.execute(
                """
                INSERT INTO audit_logs (
                    tenant_id, actor_user_id, action, resource_type,
                    resource_id, details
                )
                VALUES ($1, $2, 'BILLING_ORDER_PAID', 'billing_order',
                        $3, $4::jsonb)
                """,
                principal.tenant_id,
                principal.user_id,
                order_id,
                json.dumps({
                    "order_no": order_no,
                    "order_kind": quote["order_kind"],
                    "plan_code": quote["plan_code"],
                    "currency": currency,
                    "list_amount_minor": quote["list_amount_minor"],
                    "upgrade_credit_minor": quote["upgrade_credit_minor"],
                    "payable_amount_minor": payable,
                    "wallet_balance_after_minor": balance_after,
                }),
            )
            order = await conn.fetchrow(
                "SELECT * FROM billing_orders WHERE id = $1",
                order_id,
            )
    return _order_response(order, balance_after)


@router.put("/admin/plans/{plan_code}/prices/{currency}/{duration_days}")
async def set_plan_price(
    plan_code: str,
    currency: str,
    duration_days: int,
    payload: PlanPriceRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator permission required")
    currency = _normalize_currency(currency)
    if duration_days < 1 or duration_days > 3650:
        raise HTTPException(status_code=422, detail="Duration must be between 1 and 3650 days")

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            plan = await conn.fetchrow(
                """
                SELECT id, code, name FROM membership_plans
                WHERE code = $1 AND is_active = TRUE
                FOR UPDATE
                """,
                plan_code.strip().upper(),
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Active membership plan not found")
            previous = await conn.fetchrow(
                """
                SELECT id, amount_minor, version
                FROM membership_plan_prices
                WHERE plan_id = $1 AND currency = $2
                  AND duration_days = $3 AND is_current = TRUE
                FOR UPDATE
                """,
                plan["id"],
                currency,
                duration_days,
            )
            version = int(previous["version"]) + 1 if previous else 1
            await conn.execute(
                """
                UPDATE membership_plan_prices
                SET is_current = FALSE
                WHERE plan_id = $1 AND currency = $2 AND duration_days = $3
                  AND is_current = TRUE
                """,
                plan["id"],
                currency,
                duration_days,
            )
            price = await conn.fetchrow(
                """
                INSERT INTO membership_plan_prices (
                    plan_id, currency, duration_days, amount_minor,
                    version, is_current, created_by
                )
                VALUES ($1, $2, $3, $4, $5, TRUE, $6)
                RETURNING id, amount_minor, version, created_at
                """,
                plan["id"],
                currency,
                duration_days,
                payload.amount_minor,
                version,
                principal.user_id,
            )
            await conn.execute(
                """
                INSERT INTO platform_audit_logs (
                    actor_user_id, action, resource_type, resource_id, details
                )
                VALUES ($1, 'PLAN_PRICE_UPDATED', 'membership_plan_price',
                        $2, $3::jsonb)
                """,
                principal.user_id,
                price["id"],
                json.dumps({
                    "plan_code": plan["code"],
                    "currency": currency,
                    "duration_days": duration_days,
                    "before": (
                        {
                            "amount_minor": previous["amount_minor"],
                            "version": previous["version"],
                        }
                        if previous else None
                    ),
                    "after": {
                        "amount_minor": payload.amount_minor,
                        "version": version,
                    },
                }),
            )
    return {
        "status": "updated",
        "plan_code": plan["code"],
        "currency": currency,
        "duration_days": duration_days,
        "amount_minor": price["amount_minor"],
        "version": price["version"],
        "created_at": price["created_at"],
    }


@router.post("/admin/tenants/{tenant_id}/wallet/credits")
async def credit_tenant_wallet(
    tenant_id: UUID,
    payload: WalletCreditRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator permission required")
    currency = _normalize_currency(payload.currency)

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            tenant = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                tenant_id,
            )
            if not tenant:
                raise HTTPException(status_code=404, detail="Workspace not found")

            existing = await conn.fetchrow(
                """
                SELECT currency, direction, entry_type, amount_minor,
                       balance_after_minor, reason
                FROM wallet_ledger_entries
                WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                tenant_id,
                payload.idempotency_key,
            )
            if existing:
                if (
                    existing["currency"] != currency
                    or existing["direction"] != "CREDIT"
                    or existing["entry_type"] != "ADMIN_CREDIT"
                    or existing["amount_minor"] != payload.amount_minor
                    or existing["reason"] != payload.reason
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different wallet adjustment",
                    )
                return {
                    "status": "already_applied",
                    "currency": currency,
                    "amount_minor": payload.amount_minor,
                    "balance_after_minor": existing["balance_after_minor"],
                }

            await conn.execute(
                """
                INSERT INTO tenant_wallet_accounts (tenant_id, currency)
                VALUES ($1, $2)
                ON CONFLICT (tenant_id, currency) DO NOTHING
                """,
                tenant_id,
                currency,
            )
            wallet = await conn.fetchrow(
                """
                SELECT balance_minor FROM tenant_wallet_accounts
                WHERE tenant_id = $1 AND currency = $2
                FOR UPDATE
                """,
                tenant_id,
                currency,
            )
            balance_after = int(wallet["balance_minor"]) + payload.amount_minor
            if balance_after > 9_000_000_000_000_000:
                raise HTTPException(status_code=422, detail="Wallet balance limit exceeded")
            await conn.execute(
                """
                UPDATE tenant_wallet_accounts
                SET balance_minor = $3, updated_at = NOW()
                WHERE tenant_id = $1 AND currency = $2
                """,
                tenant_id,
                currency,
                balance_after,
            )
            await conn.execute(
                """
                INSERT INTO wallet_ledger_entries (
                    tenant_id, currency, direction, entry_type,
                    amount_minor, balance_after_minor, idempotency_key,
                    actor_user_id, reason, metadata
                )
                VALUES ($1, $2, 'CREDIT', 'ADMIN_CREDIT', $3, $4,
                        $5, $6, $7, $8::jsonb)
                """,
                tenant_id,
                currency,
                payload.amount_minor,
                balance_after,
                payload.idempotency_key,
                principal.user_id,
                payload.reason,
                json.dumps({"source": "manual_admin_credit"}),
            )
            await conn.execute(
                """
                INSERT INTO audit_logs (
                    tenant_id, actor_user_id, action, resource_type, details
                )
                VALUES ($1, $2, 'WALLET_MANUAL_CREDIT', 'wallet_account',
                        $3::jsonb)
                """,
                tenant_id,
                principal.user_id,
                json.dumps({
                    "currency": currency,
                    "amount_minor": payload.amount_minor,
                    "balance_after_minor": balance_after,
                    "reason": payload.reason,
                    "idempotency_key": payload.idempotency_key,
                }),
            )
            await conn.execute(
                """
                UPDATE subscription_renewal_intents i
                SET status = 'PENDING', next_attempt_at = NOW(),
                    last_error_code = NULL, updated_at = NOW()
                FROM tenant_auto_renew_settings ar
                JOIN tenant_subscriptions s
                  ON s.tenant_id = ar.tenant_id AND s.id = ar.subscription_id
                WHERE i.tenant_id = $1
                  AND i.currency = $2
                  AND i.status = 'WAITING_FOR_BALANCE'
                  AND ar.tenant_id = i.tenant_id
                  AND ar.subscription_id = i.subscription_id
                  AND ar.enabled = TRUE
                  AND s.status IN ('ACTIVE', 'TRIALING')
                  AND s.expires_at > NOW()
                """,
                tenant_id,
                currency,
            )
    return {
        "status": "credited",
        "currency": currency,
        "amount_minor": payload.amount_minor,
        "balance_after_minor": balance_after,
    }
