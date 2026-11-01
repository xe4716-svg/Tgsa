from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.config import settings


PASSWORD_SCRYPT_N = 2**15
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_DKLEN = 64
PASSWORD_MAXMEM = 128 * 1024 * 1024
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise ValueError("Invalid email address")
    normalized = email.strip().lower()
    if len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
        raise ValueError("Invalid email address")
    return normalized


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    if len(password) > 128:
        raise ValueError("Password is too long")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=PASSWORD_DKLEN,
        maxmem=PASSWORD_MAXMEM,
    )
    return "$".join(
        [
            "scrypt",
            str(PASSWORD_SCRYPT_N),
            str(PASSWORD_SCRYPT_R),
            str(PASSWORD_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n_value, r_value, p_value = int(n), int(r), int(p)
        if n_value > 2**17 or r_value > 16 or p_value > 4:
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_value,
            r=r_value,
            p=p_value,
            dklen=len(expected),
            maxmem=PASSWORD_MAXMEM,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _jwt_secret() -> bytes:
    secret = settings.jwt_signing_secret
    if not secret or len(secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SIGNING_SECRET must contain at least 32 characters")
    return secret.encode("utf-8")


def create_access_token(
    *,
    user_id: UUID | str,
    tenant_id: UUID | str,
    family_id: UUID | str,
    role: str,
    ttl_seconds: int | None = None,
) -> str:
    now = int(time.time())
    ttl = ttl_seconds or settings.access_token_ttl_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": "tg-saas",
        "aud": "tg-saas-api",
        "sub": str(user_id),
        "tid": str(tenant_id),
        "sid": str(family_id),
        "role": role,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(12),
    }
    head = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    signature = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url_encode(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError
        head, body, signature = token.split(".")
        header = json.loads(_b64url_decode(head))
        payload = json.loads(_b64url_decode(body))
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError
        expected = hmac.new(
            _jwt_secret(),
            f"{head}.{body}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied = _b64url_decode(signature)
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        now = int(time.time())
        if payload.get("iss") != "tg-saas" or payload.get("aud") != "tg-saas-api":
            raise ValueError
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
            raise ValueError
        if not isinstance(payload.get("iat"), int) or payload["iat"] > now + 60:
            raise ValueError
        UUID(payload["sub"])
        UUID(payload["tid"])
        UUID(payload["sid"])
        if payload.get("role") not in {"OWNER", "ADMIN", "MEMBER", "VIEWER"}:
            raise ValueError
        return payload
    except Exception as exc:
        raise ValueError("Invalid or expired access token") from exc


def create_scoped_secret(tenant_id: UUID | str) -> str:
    return f"{tenant_id}.{secrets.token_urlsafe(32)}"


def parse_scoped_secret(token: str) -> tuple[UUID, str]:
    if not isinstance(token, str) or len(token) > 512:
        raise ValueError("Invalid token")
    try:
        tenant_part, secret_part = token.split(".", 1)
        tenant_id = UUID(tenant_part)
        if len(secret_part) < 32:
            raise ValueError
        return tenant_id, token
    except Exception as exc:
        raise ValueError("Invalid token") from exc


def hash_secret(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    # Equalize the expensive password-hash path for unknown email addresses.
    return hash_password("dummy-password-for-timing-equalization")
