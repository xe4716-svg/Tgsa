from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


class PaymentGatewayError(Exception):
    """A configured payment provider could not create a payment session."""


class PaymentVerificationError(Exception):
    """A provider callback failed signature or payload verification."""


def _pem(value: str | None) -> bytes:
    if not value:
        raise PaymentGatewayError("Payment provider key is not configured")
    return value.replace("\\n", "\n").encode("utf-8")


def _minor_decimal(amount_minor: int, currency: str) -> str:
    places = 6 if currency == "USDT" else 2
    return f"{Decimal(amount_minor) / (Decimal(10) ** places):.{places}f}"


def _decimal_minor(value: str | int | Decimal, currency: str) -> int:
    places = 6 if currency.upper() == "USDT" else 2
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PaymentVerificationError("Provider amount is invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise PaymentVerificationError("Provider amount is invalid")
    scaled = amount * (Decimal(10) ** places)
    integral = scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if scaled != integral:
        raise PaymentVerificationError("Provider amount has unsupported precision")
    return int(integral)


def provider_readiness(provider: str) -> tuple[bool, str | None]:
    provider = provider.upper()
    if provider == "MANUAL":
        return True, None
    if provider == "STRIPE":
        ready = bool(settings.stripe_secret_key and settings.stripe_webhook_secret)
    elif provider == "ALIPAY":
        ready = bool(
            settings.alipay_app_id
            and settings.alipay_private_key_pem
            and settings.alipay_public_key_pem
        )
    elif provider == "WECHAT":
        ready = bool(
            settings.wechat_mchid
            and settings.wechat_appid
            and settings.wechat_serial_no
            and settings.wechat_private_key_pem
            and settings.wechat_api_v3_key
            and settings.wechat_platform_cert_pem
        )
    elif provider == "EASYPAY":
        ready = bool(
            settings.easypay_gateway_url
            and settings.easypay_pid
            and settings.easypay_key
        )
    elif provider == "USDT":
        # The TRON address allocator and confirmed-transfer watcher are a separate
        # service; an xpub alone is not enough to accept deposits safely.
        ready = False
    else:
        return False, "unsupported_provider"
    return (True, None) if ready else (False, "provider_credentials_incomplete")


def verify_stripe_event(raw_body: bytes, signature_header: str | None) -> dict[str, Any]:
    secret = settings.stripe_webhook_secret
    if not secret or not signature_header:
        raise PaymentVerificationError("Stripe webhook is not configured")
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key.strip(), []).append(value.strip())
    timestamps = parts.get("t", [])
    signatures = parts.get("v1", [])
    if not timestamps or not signatures:
        raise PaymentVerificationError("Stripe signature header is malformed")
    try:
        timestamp = int(timestamps[0])
    except ValueError as exc:
        raise PaymentVerificationError("Stripe timestamp is invalid") from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise PaymentVerificationError("Stripe webhook timestamp is outside tolerance")
    signed_payload = str(timestamp).encode() + b"." + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise PaymentVerificationError("Stripe webhook signature is invalid")
    try:
        event = json.loads(raw_body)
    except Exception as exc:
        raise PaymentVerificationError("Stripe webhook JSON is invalid") from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise PaymentVerificationError("Stripe event is incomplete")
    return event


def _alipay_signing_content(params: Mapping[str, str]) -> bytes:
    pairs = [
        (key, value)
        for key, value in params.items()
        if key not in {"sign", "sign_type"} and value not in (None, "")
    ]
    pairs.sort(key=lambda item: item[0])
    return "&".join(f"{key}={value}" for key, value in pairs).encode("utf-8")


def verify_alipay_notification(params: Mapping[str, str]) -> dict[str, Any]:
    public_pem = settings.alipay_public_key_pem
    if not public_pem or not settings.alipay_app_id:
        raise PaymentVerificationError("Alipay callback verification is not configured")
    signature = params.get("sign")
    if not signature or params.get("sign_type", "RSA2") != "RSA2":
        raise PaymentVerificationError("Alipay callback signature is missing or unsupported")
    if params.get("app_id") != settings.alipay_app_id:
        raise PaymentVerificationError("Alipay app_id does not match")
    try:
        public_key = serialization.load_pem_public_key(_pem(public_pem))
        public_key.verify(
            base64.b64decode(signature, validate=True),
            _alipay_signing_content(params),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise PaymentVerificationError("Alipay callback signature is invalid") from exc
    return dict(params)


def _wechat_platform_public_key():
    certificate = settings.wechat_platform_cert_pem
    if not certificate:
        raise PaymentVerificationError("WeChat Pay platform certificate is not configured")
    try:
        return x509.load_pem_x509_certificate(_pem(certificate)).public_key()
    except Exception as exc:
        raise PaymentVerificationError("WeChat Pay platform certificate is invalid") from exc


def verify_wechat_notification(
    raw_body: bytes,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    api_key = settings.wechat_api_v3_key
    timestamp = headers.get("Wechatpay-Timestamp") or headers.get("wechatpay-timestamp")
    nonce = headers.get("Wechatpay-Nonce") or headers.get("wechatpay-nonce")
    signature = headers.get("Wechatpay-Signature") or headers.get("wechatpay-signature")
    if not api_key or len(api_key.encode("utf-8")) != 32:
        raise PaymentVerificationError("WeChat Pay API v3 key must be 32 bytes")
    if not timestamp or not nonce or not signature:
        raise PaymentVerificationError("WeChat Pay signature headers are incomplete")
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            raise PaymentVerificationError("WeChat Pay timestamp is outside tolerance")
        message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + raw_body + b"\n"
        _wechat_platform_public_key().verify(
            base64.b64decode(signature, validate=True),
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        envelope = json.loads(raw_body)
        resource = envelope["resource"]
        plaintext = AESGCM(api_key.encode("utf-8")).decrypt(
            resource["nonce"].encode("utf-8"),
            base64.b64decode(resource["ciphertext"], validate=True),
            resource.get("associated_data", "").encode("utf-8"),
        )
        transaction = json.loads(plaintext)
    except PaymentVerificationError:
        raise
    except Exception as exc:
        raise PaymentVerificationError("WeChat Pay callback verification/decryption failed") from exc
    if not isinstance(transaction, dict):
        raise PaymentVerificationError("WeChat Pay transaction payload is invalid")
    return {"event_id": envelope.get("id"), "transaction": transaction}


def _easypay_signing_content(params: Mapping[str, str], key: str) -> str:
    pairs = [
        (name, value)
        for name, value in params.items()
        if name not in {"sign", "sign_type"} and value not in (None, "")
    ]
    pairs.sort(key=lambda item: item[0])
    return "&".join(f"{name}={value}" for name, value in pairs) + key


def verify_easypay_notification(params: Mapping[str, str]) -> dict[str, Any]:
    key = settings.easypay_key
    if not key:
        raise PaymentVerificationError("Epay-compatible gateway is not configured")
    signature = params.get("sign", "")
    if not signature or params.get("sign_type", "MD5").upper() != "MD5":
        raise PaymentVerificationError("Epay-compatible callback signature is missing")
    expected = hashlib.md5(
        _easypay_signing_content(params, key).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(expected.lower(), signature.lower()):
        raise PaymentVerificationError("Epay-compatible callback signature is invalid")
    return dict(params)


def _load_private_key(pem_value: str | None):
    try:
        return serialization.load_pem_private_key(_pem(pem_value), password=None)
    except Exception as exc:
        raise PaymentGatewayError("Payment provider private key is invalid") from exc


def _rsa2_sign(content: bytes, private_key_pem: str | None) -> str:
    signature = _load_private_key(private_key_pem).sign(
        content, padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.b64encode(signature).decode("ascii")


async def create_stripe_checkout(
    *,
    order_no: str,
    amount_minor: int,
    currency: str,
    description: str,
) -> dict[str, str]:
    if not settings.stripe_secret_key:
        raise PaymentGatewayError("Stripe secret key is not configured")
    if currency not in {"CNY", "USD"}:
        raise PaymentGatewayError("Stripe card checkout currently supports CNY and USD")
    form = {
        "mode": "payment",
        "success_url": settings.payment_return_url,
        "cancel_url": settings.payment_return_url,
        "client_reference_id": order_no,
        "metadata[order_no]": order_no,
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": str(amount_minor),
        "line_items[0][price_data][product_data][name]": description[:120],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=form,
            auth=(settings.stripe_secret_key, ""),
            headers={"Idempotency-Key": order_no},
        )
    if response.status_code >= 300:
        raise PaymentGatewayError("Stripe could not create a checkout session")
    data = response.json()
    if not data.get("id") or not data.get("url"):
        raise PaymentGatewayError("Stripe returned an incomplete checkout session")
    return {"payment_url": data["url"], "provider_reference": data["id"]}


def create_alipay_page_url(
    *,
    order_no: str,
    amount_minor: int,
    currency: str,
    description: str,
    notify_url: str,
) -> dict[str, str]:
    if currency != "CNY":
        raise PaymentGatewayError("Alipay currently supports CNY wallet top-ups only")
    if not settings.alipay_app_id or not settings.alipay_private_key_pem:
        raise PaymentGatewayError("Alipay merchant credentials are not configured")
    biz_content = {
        "out_trade_no": order_no,
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": _minor_decimal(amount_minor, currency),
        "subject": description[:128],
    }
    params = {
        "app_id": settings.alipay_app_id,
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": notify_url,
        "return_url": settings.payment_return_url,
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    params["sign"] = _rsa2_sign(_alipay_signing_content(params), settings.alipay_private_key_pem)
    return {
        "payment_url": settings.alipay_gateway_url + "?" + urlencode(params),
        "provider_reference": order_no,
    }


def _wechat_authorization(method: str, path: str, body: str) -> str:
    if not (
        settings.wechat_mchid
        and settings.wechat_serial_no
        and settings.wechat_private_key_pem
    ):
        raise PaymentGatewayError("WeChat Pay merchant credentials are incomplete")
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)[:32]
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
    signature = _rsa2_sign(message, settings.wechat_private_key_pem)
    return (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{settings.wechat_mchid}",nonce_str="{nonce}",'
        f'timestamp="{timestamp}",serial_no="{settings.wechat_serial_no}",'
        f'signature="{signature}"'
    )


async def create_wechat_native_order(
    *,
    order_no: str,
    amount_minor: int,
    currency: str,
    description: str,
    notify_url: str,
) -> dict[str, str]:
    if currency != "CNY":
        raise PaymentGatewayError("WeChat Pay Native currently supports CNY top-ups only")
    if not (settings.wechat_appid and settings.wechat_mchid):
        raise PaymentGatewayError("WeChat Pay merchant credentials are incomplete")
    path = "/v3/pay/transactions/native"
    body = json.dumps({
        "appid": settings.wechat_appid,
        "mchid": settings.wechat_mchid,
        "description": description[:127],
        "out_trade_no": order_no,
        "notify_url": notify_url,
        "amount": {"total": amount_minor, "currency": "CNY"},
    }, ensure_ascii=False, separators=(",", ":"))
    authorization = _wechat_authorization("POST", path, body)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            settings.wechat_api_base_url.rstrip("/") + path,
            content=body.encode("utf-8"),
            headers={
                "Authorization": authorization,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    if response.status_code >= 300:
        raise PaymentGatewayError("WeChat Pay could not create a Native order")
    data = response.json()
    if not data.get("code_url"):
        raise PaymentGatewayError("WeChat Pay returned no code_url")
    return {"payment_url": data["code_url"], "qr_code_url": data["code_url"], "provider_reference": order_no}


async def create_easypay_order(
    *,
    order_no: str,
    amount_minor: int,
    currency: str,
    description: str,
    channel: str,
    notify_url: str,
) -> dict[str, str]:
    if currency != "CNY":
        raise PaymentGatewayError("Epay-compatible gateway currently supports CNY only")
    if not (settings.easypay_gateway_url and settings.easypay_pid and settings.easypay_key):
        raise PaymentGatewayError("Epay-compatible gateway credentials are incomplete")
    if channel not in {"alipay", "wxpay"}:
        raise PaymentGatewayError("Epay-compatible channel must be alipay or wxpay")
    params = {
        "pid": settings.easypay_pid,
        "type": channel,
        "out_trade_no": order_no,
        "notify_url": notify_url,
        "return_url": settings.payment_return_url,
        "name": description[:100],
        "money": _minor_decimal(amount_minor, currency),
        "sign_type": "MD5",
    }
    params["sign"] = hashlib.md5(
        _easypay_signing_content(params, settings.easypay_key).encode("utf-8")
    ).hexdigest()
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(settings.easypay_gateway_url, params=params)
    if response.status_code >= 300:
        raise PaymentGatewayError("Epay-compatible gateway rejected the payment request")
    data = response.json()
    payment_url = data.get("payurl") or data.get("url")
    if data.get("code") not in (1, "1", "success", "SUCCESS") or not payment_url:
        raise PaymentGatewayError("Epay-compatible gateway returned an invalid payment URL")
    return {"payment_url": payment_url, "provider_reference": str(data.get("trade_no") or order_no)}


def stripe_payment_details(event: Mapping[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return None
    obj = (event.get("data") or {}).get("object") or {}
    if obj.get("payment_status") != "paid":
        return None
    order_no = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("order_no")
    amount = obj.get("amount_total")
    currency = str(obj.get("currency") or "").upper()
    if not order_no or amount is None or currency not in {"CNY", "USD"}:
        raise PaymentVerificationError("Stripe checkout session is missing payment details")
    return {
        "event_id": event["id"],
        "order_no": order_no,
        "provider_transaction_id": obj.get("payment_intent") or obj.get("id"),
        "amount_minor": int(amount),
        "currency": currency,
    }


def alipay_payment_details(params: Mapping[str, str]) -> dict[str, Any] | None:
    if params.get("trade_status") not in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
        return None
    order_no = params.get("out_trade_no")
    trade_no = params.get("trade_no")
    if not order_no or not trade_no or not params.get("total_amount"):
        raise PaymentVerificationError("Alipay notification is missing trade details")
    return {
        "event_id": params.get("notify_id") or f"{trade_no}:{params.get('trade_status')}",
        "order_no": order_no,
        "provider_transaction_id": trade_no,
        "amount_minor": _decimal_minor(params["total_amount"], "CNY"),
        "currency": "CNY",
    }


def wechat_payment_details(verified: Mapping[str, Any]) -> dict[str, Any] | None:
    transaction = verified["transaction"]
    if transaction.get("trade_state") != "SUCCESS":
        return None
    amount = transaction.get("amount") or {}
    order_no = transaction.get("out_trade_no")
    transaction_id = transaction.get("transaction_id")
    if not order_no or not transaction_id or amount.get("total") is None:
        raise PaymentVerificationError("WeChat Pay notification is missing trade details")
    return {
        "event_id": verified.get("event_id") or transaction_id,
        "order_no": order_no,
        "provider_transaction_id": transaction_id,
        "amount_minor": int(amount["total"]),
        "currency": str(amount.get("currency") or "CNY").upper(),
    }


def easypay_payment_details(params: Mapping[str, str]) -> dict[str, Any] | None:
    if params.get("trade_status") != "TRADE_SUCCESS":
        return None
    order_no = params.get("out_trade_no")
    trade_no = params.get("trade_no")
    money = params.get("money")
    if not order_no or not trade_no or not money:
        raise PaymentVerificationError("Epay-compatible notification is missing trade details")
    return {
        "event_id": f"{trade_no}:{params.get('trade_status')}",
        "order_no": order_no,
        "provider_transaction_id": trade_no,
        "amount_minor": _decimal_minor(money, "CNY"),
        "currency": "CNY",
    }
