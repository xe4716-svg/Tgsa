from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.billing.payment_gateway import (
    PaymentGatewayError,
    PaymentVerificationError,
    alipay_payment_details,
    create_alipay_page_url,
    create_easypay_order,
    create_stripe_checkout,
    create_wechat_native_order,
    easypay_payment_details,
    provider_readiness,
    stripe_payment_details,
    verify_alipay_notification,
    verify_easypay_notification,
    verify_stripe_event,
    verify_wechat_notification,
    wechat_payment_details,
)


router = APIRouter(tags=["payment orchestration"])


class WalletTopupRequest(BaseModel):
    provider: str = Field(min_length=3, max_length=16)
    currency: str = Field(min_length=3, max_length=8)
    amount_minor: int = Field(gt=0, le=9_000_000_000_000_000)
    idempotency_key: str = Field(min_length=16, max_length=120)
    channel: str | None = Field(default=None, max_length=16)


class PaymentProviderUpdateRequest(BaseModel):
    enabled: bool
    min_amount_minor: int = Field(gt=0, le=9_000_000_000_000_000)
    max_amount_minor: int = Field(gt=0, le=9_000_000_000_000_000)
    sort_order: int = Field(default=100, ge=0, le=100000)
    public_instructions: str = Field(default="", max_length=1000)


def _require_platform_admin(principal: AuthPrincipal) -> None:
    if not principal.platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator permission required")


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value or []


def _topup_response(row) -> dict:
    return {
        "id": str(row["id"]),
        "order_no": row["order_no"],
        "provider": row["provider"],
        "currency": row["currency"],
        "amount_minor": row["amount_minor"],
        "status": row["status"],
        "payment_url": row["payment_url"],
        "qr_code_url": row["qr_code_url"],
        "expires_at": row["expires_at"],
        "paid_at": row["paid_at"],
        "created_at": row["created_at"],
    }


def _fingerprint(provider: str, currency: str, amount_minor: int, channel: str | None) -> str:
    raw = f"{provider}|{currency}|{amount_minor}|{channel or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _notify_url(request: Request, provider: str) -> str:
    from app.config import settings

    base = settings.public_api_base_url.rstrip("/")
    return f"{base}/webhooks/{provider.lower()}"


async def _create_provider_payment(
    provider: str,
    order_no: str,
    amount_minor: int,
    currency: str,
    channel: str | None,
    request: Request,
) -> dict[str, str]:
    description = "TGMate wallet top-up"
    notify_url = _notify_url(request, provider)
    if provider == "STRIPE":
        return await create_stripe_checkout(
            order_no=order_no,
            amount_minor=amount_minor,
            currency=currency,
            description=description,
        )
    if provider == "ALIPAY":
        return create_alipay_page_url(
            order_no=order_no,
            amount_minor=amount_minor,
            currency=currency,
            description=description,
            notify_url=notify_url,
        )
    if provider == "WECHAT":
        return await create_wechat_native_order(
            order_no=order_no,
            amount_minor=amount_minor,
            currency=currency,
            description=description,
            notify_url=notify_url,
        )
    if provider == "EASYPAY":
        return await create_easypay_order(
            order_no=order_no,
            amount_minor=amount_minor,
            currency=currency,
            description=description,
            channel=channel or "alipay",
            notify_url=notify_url,
        )
    raise PaymentGatewayError("Selected payment provider is not available")


@router.get("/billing/payment-methods")
async def list_payment_methods(request: Request):
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT provider, enabled, display_name, currencies,
                   min_amount_minor, max_amount_minor, sort_order,
                   public_instructions
            FROM payment_provider_configs
            ORDER BY sort_order, provider
            """
        )
    from app.config import settings

    methods = []
    for row in rows:
        provider = row["provider"]
        ready, reason = provider_readiness(provider)
        methods.append({
            "provider": provider,
            "name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "available": bool(row["enabled"]) and ready,
            "unavailable_reason": reason if row["enabled"] and not ready else None,
            "currencies": _json_value(row["currencies"]),
            "min_amount_minor": row["min_amount_minor"],
            "max_amount_minor": row["max_amount_minor"],
            "sort_order": row["sort_order"],
            "instructions": row["public_instructions"],
            "contact_url": settings.manual_payment_contact_url if provider == "MANUAL" else None,
        })
    return {"methods": methods}


@router.get("/admin/billing/payment-methods")
async def admin_list_payment_methods(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    async with request.app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT provider, enabled, display_name, currencies,
                   min_amount_minor, max_amount_minor, sort_order,
                   public_instructions, updated_at
            FROM payment_provider_configs
            ORDER BY sort_order, provider
            """
        )
    result = []
    for row in rows:
        ready, reason = provider_readiness(row["provider"])
        result.append({
            "provider": row["provider"],
            "enabled": row["enabled"],
            "credentials_ready": ready,
            "readiness_issue": reason,
            "display_name": row["display_name"],
            "currencies": _json_value(row["currencies"]),
            "min_amount_minor": row["min_amount_minor"],
            "max_amount_minor": row["max_amount_minor"],
            "sort_order": row["sort_order"],
            "public_instructions": row["public_instructions"],
            "updated_at": row["updated_at"],
        })
    return {"methods": result}


@router.put("/admin/billing/payment-methods/{provider}")
async def update_payment_method(
    provider: str,
    payload: PaymentProviderUpdateRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    _require_platform_admin(principal)
    provider = provider.strip().upper()
    if payload.max_amount_minor < payload.min_amount_minor:
        raise HTTPException(status_code=422, detail="Maximum amount must be at least the minimum amount")
    async with request.app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE payment_provider_configs
            SET enabled = $2, min_amount_minor = $3, max_amount_minor = $4,
                sort_order = $5, public_instructions = $6,
                updated_by = $7, updated_at = NOW()
            WHERE provider = $1
            RETURNING provider, enabled, min_amount_minor, max_amount_minor,
                      sort_order, public_instructions
            """,
            provider,
            payload.enabled,
            payload.min_amount_minor,
            payload.max_amount_minor,
            payload.sort_order,
            payload.public_instructions,
            principal.user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Payment provider not found")
    ready, reason = provider_readiness(provider)
    return {
        "status": "updated",
        "provider": row["provider"],
        "enabled": row["enabled"],
        "credentials_ready": ready,
        "readiness_issue": reason,
        "min_amount_minor": row["min_amount_minor"],
        "max_amount_minor": row["max_amount_minor"],
    }


@router.post("/billing/wallet/topups")
async def create_wallet_topup(
    payload: WalletTopupRequest,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    provider = payload.provider.strip().upper()
    currency = payload.currency.strip().upper()
    if provider not in {"STRIPE", "ALIPAY", "WECHAT", "EASYPAY"}:
        if provider == "MANUAL":
            raise HTTPException(
                status_code=409,
                detail="For manual payment, contact platform support; an administrator will credit the wallet after verifying receipt.",
            )
        if provider == "USDT":
            raise HTTPException(
                status_code=503,
                detail="TRC20 deposit address allocation and confirmed-chain monitoring are not enabled yet.",
            )
        raise HTTPException(status_code=422, detail="Unsupported payment provider")
    if currency not in {"CNY", "USD"}:
        raise HTTPException(status_code=422, detail="This payment provider does not support the selected currency")
    if provider in {"ALIPAY", "WECHAT", "EASYPAY"} and currency != "CNY":
        raise HTTPException(status_code=422, detail="Selected provider supports CNY only")
    if provider == "EASYPAY" and (payload.channel or "alipay").lower() not in {"alipay", "wxpay"}:
        raise HTTPException(status_code=422, detail="Epay channel must be alipay or wxpay")
    channel = (payload.channel or "alipay").lower() if provider == "EASYPAY" else None
    ready, reason = provider_readiness(provider)
    if not ready:
        raise HTTPException(status_code=503, detail={"message": "Payment provider is not configured", "reason": reason})

    fingerprint = _fingerprint(provider, currency, payload.amount_minor, channel)
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                principal.tenant_id,
            )
            config = await conn.fetchrow(
                """
                SELECT enabled, currencies, min_amount_minor, max_amount_minor
                FROM payment_provider_configs
                WHERE provider = $1
                """,
                provider,
            )
            if not config or not config["enabled"]:
                raise HTTPException(status_code=409, detail="Payment method is disabled by the platform")
            currencies = _json_value(config["currencies"])
            if currency not in currencies:
                raise HTTPException(status_code=422, detail="Currency is not enabled for this payment method")
            if not config["min_amount_minor"] <= payload.amount_minor <= config["max_amount_minor"]:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Top-up amount is outside the allowed range",
                        "minimum_minor": config["min_amount_minor"],
                        "maximum_minor": config["max_amount_minor"],
                    },
                )
            existing = await conn.fetchrow(
                """
                SELECT * FROM wallet_topup_orders
                WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                principal.tenant_id,
                payload.idempotency_key,
            )
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different top-up",
                    )
                if existing["status"] != "PENDING":
                    return _topup_response(existing)
                order = existing
            else:
                order_no = uuid4().hex
                order = await conn.fetchrow(
                    """
                    INSERT INTO wallet_topup_orders (
                        tenant_id, order_no, provider, idempotency_key,
                        request_fingerprint, currency, amount_minor,
                        created_by, expires_at, metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        NOW() + INTERVAL '30 minutes', $9::jsonb
                    )
                    RETURNING *
                    """,
                    principal.tenant_id,
                    order_no,
                    provider,
                    payload.idempotency_key,
                    fingerprint,
                    currency,
                    payload.amount_minor,
                    principal.user_id,
                    json.dumps({"channel": channel} if channel else {}),
                )

    if order["payment_url"]:
        return _topup_response(order)

    try:
        provider_result = await _create_provider_payment(
            provider,
            order["order_no"],
            payload.amount_minor,
            currency,
            channel,
            request,
        )
    except (PaymentGatewayError, httpx.HTTPError) as exc:
        async with request.app.state.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(principal.tenant_id),
                )
                await conn.execute(
                    """
                    UPDATE wallet_topup_orders
                    SET status = 'FAILED', updated_at = NOW(),
                        metadata = metadata || $3::jsonb
                    WHERE tenant_id = $1 AND id = $2 AND status = 'PENDING'
                    """,
                    principal.tenant_id,
                    order["id"],
                    json.dumps({"provider_error": type(exc).__name__}),
                )
        raise HTTPException(status_code=502, detail="Payment provider could not create a payment session") from exc

    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            updated = await conn.fetchrow(
                """
                UPDATE wallet_topup_orders
                SET payment_url = $3, qr_code_url = $4,
                    metadata = metadata || $5::jsonb, updated_at = NOW()
                WHERE tenant_id = $1 AND id = $2 AND status = 'PENDING'
                RETURNING *
                """,
                principal.tenant_id,
                order["id"],
                provider_result.get("payment_url"),
                provider_result.get("qr_code_url"),
                json.dumps({"provider_reference": provider_result.get("provider_reference")}),
            )
    if not updated:
        raise HTTPException(status_code=409, detail="Top-up order changed while payment was being prepared")
    return _topup_response(updated)



@router.get("/billing/wallet/topups")
async def list_wallet_topups(
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
                SELECT * FROM wallet_topup_orders
                WHERE tenant_id = $1
                ORDER BY created_at DESC
                LIMIT 100
                """,
                principal.tenant_id,
            )
    return {"orders": [_topup_response(row) for row in rows]}


@router.get("/billing/wallet/topups/{order_no}")
async def get_wallet_topup(
    order_no: str,
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
):
    async with request.app.state.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            row = await conn.fetchrow(
                """
                SELECT * FROM wallet_topup_orders
                WHERE tenant_id = $1 AND order_no = $2
                """,
                principal.tenant_id,
                order_no,
            )
    if not row:
        raise HTTPException(status_code=404, detail="Top-up order not found")
    return _topup_response(row)


async def _process_verified_payment(
    request: Request,
    provider: str,
    details: dict,
    raw_body: bytes,
) -> str:
    order_no = str(details["order_no"])
    event_id = str(details["event_id"])
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """
                INSERT INTO payment_webhook_events (
                    provider, event_id, order_no, payload_sha256
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (provider, event_id) DO NOTHING
                RETURNING id
                """,
                provider,
                event_id[:200],
                order_no[:48],
                payload_hash,
            )
            if not inserted:
                prior = await conn.fetchrow(
                    """
                    SELECT payload_sha256, status FROM payment_webhook_events
                    WHERE provider = $1 AND event_id = $2
                    """,
                    provider,
                    event_id[:200],
                )
                if prior and prior["payload_sha256"] != payload_hash:
                    return "event_collision"
                return "duplicate_event"

            routed = await conn.fetchrow(
                "SELECT * FROM resolve_wallet_topup_order($1)",
                order_no,
            )
            if not routed:
                await conn.execute(
                    """
                    UPDATE payment_webhook_events
                    SET status = 'FAILED', failure_code = 'ORDER_NOT_FOUND',
                        processed_at = NOW()
                    WHERE id = $1
                    """,
                    inserted["id"],
                )
                return "order_not_found"

            if (
                routed["provider"] != provider
                or routed["currency"] != details["currency"]
                or routed["amount_minor"] != int(details["amount_minor"])
            ):
                await conn.execute(
                    """
                    UPDATE payment_webhook_events
                    SET status = 'FAILED', failure_code = 'PAYMENT_MISMATCH',
                        processed_at = NOW()
                    WHERE id = $1
                    """,
                    inserted["id"],
                )
                return "payment_mismatch"

            tenant_id = routed["tenant_id"]
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1 FOR UPDATE",
                tenant_id,
            )
            topup = await conn.fetchrow(
                """
                SELECT * FROM wallet_topup_orders
                WHERE tenant_id = $1 AND id = $2
                FOR UPDATE
                """,
                tenant_id,
                routed["id"],
            )
            if not topup:
                await conn.execute(
                    """
                    UPDATE payment_webhook_events
                    SET status = 'FAILED', failure_code = 'ORDER_NOT_FOUND',
                        processed_at = NOW()
                    WHERE id = $1
                    """,
                    inserted["id"],
                )
                return "order_not_found"
            if topup["status"] == "PAID":
                await conn.execute(
                    """
                    UPDATE payment_webhook_events
                    SET status = 'PROCESSED', processed_at = NOW()
                    WHERE id = $1
                    """,
                    inserted["id"],
                )
                return "already_paid"
            if topup["status"] in {"FAILED", "CANCELED"}:
                await conn.execute(
                    """
                    UPDATE payment_webhook_events
                    SET status = 'FAILED', failure_code = 'ORDER_CLOSED',
                        processed_at = NOW()
                    WHERE id = $1
                    """,
                    inserted["id"],
                )
                return "order_closed"

            await conn.execute(
                """
                INSERT INTO tenant_wallet_accounts (tenant_id, currency)
                VALUES ($1, $2)
                ON CONFLICT (tenant_id, currency) DO NOTHING
                """,
                tenant_id,
                topup["currency"],
            )
            wallet = await conn.fetchrow(
                """
                SELECT balance_minor FROM tenant_wallet_accounts
                WHERE tenant_id = $1 AND currency = $2
                FOR UPDATE
                """,
                tenant_id,
                topup["currency"],
            )
            balance_after = int(wallet["balance_minor"]) + int(topup["amount_minor"])
            if balance_after > 9_000_000_000_000_000:
                await conn.execute(
                    """
                    UPDATE payment_webhook_events
                    SET status = 'FAILED', failure_code = 'WALLET_LIMIT',
                        processed_at = NOW()
                    WHERE id = $1
                    """,
                    inserted["id"],
                )
                return "wallet_limit"

            await conn.execute(
                """
                UPDATE tenant_wallet_accounts
                SET balance_minor = $3, updated_at = NOW()
                WHERE tenant_id = $1 AND currency = $2
                """,
                tenant_id,
                topup["currency"],
                balance_after,
            )
            await conn.execute(
                """
                INSERT INTO wallet_ledger_entries (
                    tenant_id, currency, direction, entry_type,
                    amount_minor, balance_after_minor, reference_type,
                    reference_id, idempotency_key, reason, metadata
                )
                VALUES ($1, $2, 'CREDIT', 'PAYMENT_CREDIT', $3, $4,
                        'WALLET_TOPUP', $5, $6, $7, $8::jsonb)
                """,
                tenant_id,
                topup["currency"],
                topup["amount_minor"],
                balance_after,
                topup["id"],
                f"payment-topup:{topup['id']}",
                f"Wallet top-up via {provider}",
                json.dumps({
                    "provider": provider,
                    "provider_transaction_id": details.get("provider_transaction_id"),
                    "order_no": order_no,
                }),
            )
            db_now = await conn.fetchval("SELECT NOW()")
            await conn.execute(
                """
                UPDATE wallet_topup_orders
                SET status = 'PAID', provider_transaction_id = $3,
                    paid_at = $4, updated_at = $4,
                    metadata = metadata || $5::jsonb
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                topup["id"],
                details.get("provider_transaction_id"),
                db_now,
                json.dumps({"paid_after_expiry": topup["expires_at"] < db_now}),
            )
            await conn.execute(
                """
                INSERT INTO audit_logs (
                    tenant_id, actor_user_id, action, resource_type,
                    resource_id, details
                )
                VALUES ($1, NULL, 'WALLET_TOPUP_PAID', 'wallet_topup_order',
                        $2, $3::jsonb)
                """,
                tenant_id,
                topup["id"],
                json.dumps({
                    "provider": provider,
                    "order_no": order_no,
                    "currency": topup["currency"],
                    "amount_minor": topup["amount_minor"],
                    "balance_after_minor": balance_after,
                    "provider_transaction_id": details.get("provider_transaction_id"),
                }),
            )
            await conn.execute(
                """
                UPDATE payment_webhook_events
                SET status = 'PROCESSED', processed_at = NOW()
                WHERE id = $1
                """,
                inserted["id"],
            )
    return "credited"


def _parse_form_request(request: Request, raw_body: bytes) -> dict[str, str]:
    values = {key: value for key, value in request.query_params.items()}
    if raw_body:
        parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
        values.update({key: item[-1] for key, item in parsed.items() if item})
    return values


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    raw = await request.body()
    try:
        event = verify_stripe_event(raw, request.headers.get("Stripe-Signature"))
        details = stripe_payment_details(event)
        if details is None:
            return {"received": True, "ignored": True}
        result = await _process_verified_payment(request, "STRIPE", details, raw)
    except PaymentVerificationError as exc:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook") from exc
    return {"received": True, "result": result}


@router.post("/webhooks/alipay")
async def alipay_webhook(request: Request):
    raw = await request.body()
    try:
        params = _parse_form_request(request, raw)
        verified = verify_alipay_notification(params)
        details = alipay_payment_details(verified)
        if details is not None:
            await _process_verified_payment(request, "ALIPAY", details, raw)
    except PaymentVerificationError:
        return PlainTextResponse("failure", status_code=400)
    return PlainTextResponse("success")


@router.post("/webhooks/wechat")
async def wechat_webhook(request: Request):
    raw = await request.body()
    try:
        verified = verify_wechat_notification(raw, request.headers)
        details = wechat_payment_details(verified)
        if details is not None:
            await _process_verified_payment(request, "WECHAT", details, raw)
    except PaymentVerificationError:
        return JSONResponse(
            status_code=400,
            content={"code": "FAIL", "message": "signature or payload verification failed"},
        )
    return {"code": "SUCCESS", "message": "成功"}


@router.api_route("/webhooks/easypay", methods=["GET", "POST"])
async def easypay_webhook(request: Request):
    raw = await request.body()
    try:
        params = _parse_form_request(request, raw)
        verified = verify_easypay_notification(params)
        details = easypay_payment_details(verified)
        if details is not None:
            await _process_verified_payment(request, "EASYPAY", details, raw or urlencode_bytes(params))
    except PaymentVerificationError:
        return PlainTextResponse("fail", status_code=400)
    return PlainTextResponse("success")


def urlencode_bytes(params: dict[str, str]) -> bytes:
    from urllib.parse import urlencode
    return urlencode(params).encode("utf-8")
