from uuid import uuid4

import pytest

from app.auth.security import (
    create_access_token,
    create_scoped_secret,
    decode_access_token,
    hash_password,
    hash_secret,
    normalize_email,
    parse_scoped_secret,
    verify_password,
)
from app.config import settings


def test_password_hash_round_trip():
    encoded = hash_password("a-strong-password-123")
    assert encoded.startswith("scrypt$")
    assert verify_password("a-strong-password-123", encoded)
    assert not verify_password("wrong-password-123", encoded)


def test_password_policy():
    with pytest.raises(ValueError):
        hash_password("short")


def test_email_normalization():
    assert normalize_email("  USER+test@Example.COM ") == "user+test@example.com"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")


def test_scoped_secret_contains_tenant_and_is_hashable():
    tenant_id = uuid4()
    token = create_scoped_secret(tenant_id)
    parsed_tenant, raw = parse_scoped_secret(token)
    assert parsed_tenant == tenant_id
    assert raw == token
    assert len(hash_secret(token)) == 64


def _run_with_test_secret(callback):
    previous = settings.jwt_signing_secret
    object.__setattr__(settings, "jwt_signing_secret", "test-only-secret-with-32-or-more-characters")
    try:
        callback()
    finally:
        object.__setattr__(settings, "jwt_signing_secret", previous)


def test_access_token_round_trip_and_claims():
    def check():
        user_id, tenant_id, family_id = uuid4(), uuid4(), uuid4()
        token = create_access_token(
            user_id=user_id,
            tenant_id=tenant_id,
            family_id=family_id,
            role="OWNER",
        )
        claims = decode_access_token(token)
        assert claims["sub"] == str(user_id)
        assert claims["tid"] == str(tenant_id)
        assert claims["sid"] == str(family_id)
        assert claims["role"] == "OWNER"
    _run_with_test_secret(check)


def test_access_token_rejects_tampering():
    def check():
        token = create_access_token(
            user_id=uuid4(),
            tenant_id=uuid4(),
            family_id=uuid4(),
            role="MEMBER",
        )
        parts = token.split(".")
        parts[1] = parts[1][:-1] + ("A" if parts[1][-1] != "A" else "B")
        with pytest.raises(ValueError):
            decode_access_token(".".join(parts))
    _run_with_test_secret(check)
