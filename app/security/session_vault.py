from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedSession:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    kms_key_id: str
    encryption_version: int = 1


class SessionVault:
    """Envelope-encrypt Telegram session strings using AES-256-GCM.

    The master key is a 32-byte key supplied by a KMS/secret manager.
    A fresh data-encryption key (DEK) is generated per account session.
    The DEK is wrapped by the master key; session ciphertext is encrypted
    with the DEK. Tenant/account IDs are authenticated as associated data.
    """

    VERSION = 1
    KEY_BYTES = 32
    NONCE_BYTES = 12

    def __init__(self, master_key: bytes, key_id: str):
        if len(master_key) != self.KEY_BYTES:
            raise ValueError("Session master key must be exactly 32 bytes")
        if not key_id or len(key_id) > 255:
            raise ValueError("Invalid key identifier")
        self._master_key = master_key
        self.key_id = key_id

    @classmethod
    def from_base64(cls, encoded_key: str, key_id: str) -> "SessionVault":
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except Exception as exc:
            raise ValueError("SESSION_KEK_BASE64 is not valid base64") from exc
        return cls(key, key_id)

    @staticmethod
    def _session_aad(tenant_id: UUID, account_id: UUID) -> bytes:
        return f"tgs:telegram-session:v1:{tenant_id}:{account_id}".encode()

    @staticmethod
    def _dek_aad(tenant_id: UUID, account_id: UUID) -> bytes:
        return f"tgs:telegram-session-dek:v1:{tenant_id}:{account_id}".encode()

    def encrypt(
        self, tenant_id: UUID, account_id: UUID, session_string: str
    ) -> EncryptedSession:
        if not isinstance(session_string, str) or not session_string.strip():
            raise ValueError("Session string must not be empty")

        dek = AESGCM.generate_key(bit_length=256)
        data_nonce = os.urandom(self.NONCE_BYTES)
        wrap_nonce = os.urandom(self.NONCE_BYTES)

        ciphertext = AESGCM(dek).encrypt(
            data_nonce,
            session_string.encode("utf-8"),
            self._session_aad(tenant_id, account_id),
        )
        wrapped_dek = wrap_nonce + AESGCM(self._master_key).encrypt(
            wrap_nonce,
            dek,
            self._dek_aad(tenant_id, account_id),
        )
        return EncryptedSession(
            ciphertext=ciphertext,
            nonce=data_nonce,
            wrapped_dek=wrapped_dek,
            kms_key_id=self.key_id,
            encryption_version=self.VERSION,
        )

    def decrypt(
        self,
        tenant_id: UUID,
        account_id: UUID,
        record: EncryptedSession,
    ) -> str:
        if record.encryption_version != self.VERSION:
            raise ValueError("Unsupported session encryption version")
        if record.kms_key_id != self.key_id:
            raise ValueError("Session was encrypted with a different key version")
        if len(record.wrapped_dek) <= self.NONCE_BYTES:
            raise ValueError("Invalid wrapped data key")

        wrap_nonce = record.wrapped_dek[: self.NONCE_BYTES]
        wrapped_key_ciphertext = record.wrapped_dek[self.NONCE_BYTES :]
        dek = AESGCM(self._master_key).decrypt(
            wrap_nonce,
            wrapped_key_ciphertext,
            self._dek_aad(tenant_id, account_id),
        )
        plaintext = AESGCM(dek).decrypt(
            record.nonce,
            record.ciphertext,
            self._session_aad(tenant_id, account_id),
        )
        return plaintext.decode("utf-8")
