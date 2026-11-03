"""Versioned, tenant-bound envelope encryption for proxy authentication."""
import base64
import json
from uuid import UUID

from app.security.session_vault import EncryptedSession, SessionVault


class ProxyVault(SessionVault):
    @staticmethod
    def _session_aad(tenant_id: UUID, account_id: UUID) -> bytes:
        return f'tgs:proxy-credentials:v1:{tenant_id}:{account_id}'.encode()

    @staticmethod
    def _dek_aad(tenant_id: UUID, account_id: UUID) -> bytes:
        return f'tgs:proxy-credentials-dek:v1:{tenant_id}:{account_id}'.encode()

    def encode_credentials(self, tenant_id, proxy_id, credentials):
        allowed = {'username', 'password', 'secret'}
        if set(credentials) - allowed:
            raise ValueError('Unsupported proxy credential fields')
        record = self.encrypt(tenant_id, proxy_id, json.dumps(credentials))
        return json.dumps({
            'v': 1, 'key_id': record.kms_key_id,
            **{key: base64.b64encode(getattr(record, key)).decode()
               for key in ('ciphertext', 'nonce', 'wrapped_dek')},
        }).encode()

    def decode_credentials(self, tenant_id, proxy_id, blob):
        try:
            data = json.loads(bytes(blob))
            record = EncryptedSession(
                **{key: base64.b64decode(data[key], validate=True)
                   for key in ('ciphertext', 'nonce', 'wrapped_dek')},
                kms_key_id=data['key_id'], encryption_version=data['v'],
            )
            credentials = json.loads(self.decrypt(tenant_id, proxy_id, record))
            if not isinstance(credentials, dict) or set(credentials) - {'username', 'password', 'secret'}:
                raise ValueError()
            if any(not isinstance(value, str) for value in credentials.values()):
                raise ValueError()
            return credentials
        except Exception:
            raise ValueError('Proxy credentials cannot be decrypted') from None
