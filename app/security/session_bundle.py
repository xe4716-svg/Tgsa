from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = "TGSAAS_SESSION_BUNDLE"
FORMAT_VERSION = 1
MAX_ACCOUNTS = 100
MAX_FILE_BYTES = 10 * 1024 * 1024
MIN_PASSWORD_LENGTH = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


@dataclass(frozen=True)
class SessionBundleAccount:
    session: str
    phone_masked: str | None = None
    username: str | None = None
    display_name: str | None = None
    telegram_user_id: int | None = None


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def export_bundle(accounts: Iterable[SessionBundleAccount], password: str) -> bytes:
    """Create a password-encrypted multi-account .tgsession bundle.

    Raw Telethon session strings are never written in plaintext to disk.
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Export password must be at least {MIN_PASSWORD_LENGTH} characters")

    items = list(accounts)
    if not items or len(items) > MAX_ACCOUNTS:
        raise ValueError(f"Bundle must contain between 1 and {MAX_ACCOUNTS} accounts")

    payload = {
        "version": FORMAT_VERSION,
        "accounts": [
            {
                "session": item.session,
                "phone_masked": item.phone_masked,
                "username": item.username,
                "display_name": item.display_name,
                "telegram_user_id": item.telegram_user_id,
            }
            for item in items
        ],
    }
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    aad = f"{MAGIC}:v{FORMAT_VERSION}".encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = {
        "magic": MAGIC,
        "version": FORMAT_VERSION,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": _b64e(salt),
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ciphertext),
    }
    encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("Session bundle exceeds maximum file size")
    return encoded


def import_bundle(data: bytes, password: str) -> list[SessionBundleAccount]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Invalid session bundle size")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    try:
        envelope = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid session bundle format") from exc

    if (
        envelope.get("magic") != MAGIC
        or envelope.get("version") != FORMAT_VERSION
        or envelope.get("kdf") != "scrypt"
        or envelope.get("n") != SCRYPT_N
        or envelope.get("r") != SCRYPT_R
        or envelope.get("p") != SCRYPT_P
    ):
        raise ValueError("Unsupported session bundle format")

    salt = _b64d(envelope["salt"])
    nonce = _b64d(envelope["nonce"])
    ciphertext = _b64d(envelope["ciphertext"])
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError("Invalid session bundle cryptographic parameters")

    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    aad = f"{MAGIC}:v{FORMAT_VERSION}".encode()
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise ValueError("Incorrect password or damaged session bundle") from exc

    try:
        payload = json.loads(plaintext.decode("utf-8"))
        if payload.get("version") != FORMAT_VERSION:
            raise ValueError("Unsupported payload version")
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list) or not 1 <= len(raw_accounts) <= MAX_ACCOUNTS:
            raise ValueError("Invalid account list")
        result = []
        for item in raw_accounts:
            session = item.get("session")
            if not isinstance(session, str) or not session.strip():
                raise ValueError("Invalid session entry")
            result.append(
                SessionBundleAccount(
                    session=session,
                    phone_masked=item.get("phone_masked"),
                    username=item.get("username"),
                    display_name=item.get("display_name"),
                    telegram_user_id=item.get("telegram_user_id"),
                )
            )
        return result
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid decrypted session payload") from exc
