import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

from app.billing import payment_gateway as gateway


def test_stripe_webhook_signature_and_payment_details(monkeypatch):
    secret = "whsec_test_secret"
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(stripe_webhook_secret=secret))
    event = {
        "id": "evt_123",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_123",
                "client_reference_id": "topup_order_123",
                "payment_intent": "pi_123",
                "payment_status": "paid",
                "amount_total": 2500,
                "currency": "usd",
            }
        },
    }
    raw = json.dumps(event, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    verified = gateway.verify_stripe_event(
        raw, f"t={timestamp},v1={signature}"
    )
    details = gateway.stripe_payment_details(verified)
    assert details == {
        "event_id": "evt_123",
        "order_no": "topup_order_123",
        "provider_transaction_id": "pi_123",
        "amount_minor": 2500,
        "currency": "USD",
    }


def test_stripe_rejects_tampered_payload(monkeypatch):
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(stripe_webhook_secret="whsec_test"))
    raw = b'{"id":"evt_1","type":"checkout.session.completed"}'
    timestamp = str(int(time.time()))
    with pytest.raises(gateway.PaymentVerificationError):
        gateway.verify_stripe_event(raw, f"t={timestamp},v1=" + "0" * 64)


def _make_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, private_pem, public_pem


def test_alipay_rsa2_notification_signature(monkeypatch):
    private_key, _, public_pem = _make_rsa_keypair()
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(
        alipay_app_id="app_123", alipay_public_key_pem=public_pem
    ))
    params = {
        "app_id": "app_123",
        "out_trade_no": "order_123",
        "trade_no": "alipay_trade_123",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "12.34",
        "sign_type": "RSA2",
    }
    signature = private_key.sign(
        gateway._alipay_signing_content(params),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    params["sign"] = base64.b64encode(signature).decode()
    verified = gateway.verify_alipay_notification(params)
    details = gateway.alipay_payment_details(verified)
    assert details["order_no"] == "order_123"
    assert details["amount_minor"] == 1234
    assert details["currency"] == "CNY"


def test_alipay_rejects_wrong_app_id(monkeypatch):
    _, _, public_pem = _make_rsa_keypair()
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(
        alipay_app_id="expected_app", alipay_public_key_pem=public_pem
    ))
    with pytest.raises(gateway.PaymentVerificationError):
        gateway.verify_alipay_notification({
            "app_id": "different_app",
            "sign": "invalid",
            "sign_type": "RSA2",
        })


def test_wechat_signature_and_aes_gcm_decryption(monkeypatch):
    private_key, _, _ = _make_rsa_keypair()
    api_key = "12345678901234567890123456789012"
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(wechat_api_v3_key=api_key))

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "CI WeChat Platform Test")
    ])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    gateway.settings.wechat_platform_cert_pem = certificate.public_bytes(
        serialization.Encoding.PEM
    ).decode()

    transaction = {
        "out_trade_no": "wechat_order_123",
        "transaction_id": "wx_tx_123",
        "trade_state": "SUCCESS",
        "amount": {"total": 5000, "currency": "CNY"},
    }
    nonce = b"0123456789ab"
    associated_data = b"transaction"
    ciphertext = AESGCM(api_key.encode()).encrypt(
        nonce, json.dumps(transaction).encode(), associated_data
    )
    envelope = {
        "id": "wx_event_123",
        "resource": {
            "algorithm": "AEAD_AES_256_GCM",
            "nonce": nonce.decode(),
            "associated_data": associated_data.decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    }
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request_nonce = "request_nonce_123"
    message = (
        timestamp.encode() + b"\n" + request_nonce.encode()
        + b"\n" + raw + b"\n"
    )
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    verified = gateway.verify_wechat_notification(raw, {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": request_nonce,
        "Wechatpay-Signature": base64.b64encode(signature).decode(),
    })
    details = gateway.wechat_payment_details(verified)
    assert details["order_no"] == "wechat_order_123"
    assert details["amount_minor"] == 5000
    assert details["provider_transaction_id"] == "wx_tx_123"


def test_easypay_md5_callback_signature(monkeypatch):
    key = "epay_test_key"
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(easypay_key=key))
    params = {
        "pid": "10001",
        "out_trade_no": "epay_order_123",
        "trade_no": "gateway_trade_123",
        "trade_status": "TRADE_SUCCESS",
        "money": "99.90",
        "sign_type": "MD5",
    }
    params["sign"] = hashlib.md5(
        gateway._easypay_signing_content(params, key).encode()
    ).hexdigest()
    verified = gateway.verify_easypay_notification(params)
    details = gateway.easypay_payment_details(verified)
    assert details["amount_minor"] == 9990
    assert details["currency"] == "CNY"


def test_currency_minor_unit_conversion_is_exact():
    assert gateway._decimal_minor("1.234567", "USDT") == 1_234_567
    assert gateway._decimal_minor("12.34", "CNY") == 1234
    with pytest.raises(gateway.PaymentVerificationError):
        gateway._decimal_minor("12.345", "CNY")


def test_usdt_provider_is_not_reported_ready_without_chain_watcher(monkeypatch):
    monkeypatch.setattr(gateway, "settings", SimpleNamespace(
        tron_account_xpub="xpub-example", trongrid_api_key="api-key"
    ))
    ready, reason = gateway.provider_readiness("USDT")
    assert ready is False
    assert reason == "unsupported_provider"
