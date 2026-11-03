from uuid import uuid4
import pytest
from app.security.proxy_vault import ProxyVault


def test_proxy_credentials_are_encrypted_and_bound_to_tenant_and_profile():
    vault = ProxyVault(b'x'*32, 'test-key')
    tenant, proxy = uuid4(), uuid4()
    credentials = {'username':'user', 'password':'private-password', 'secret':'proxy-secret'}
    blob = vault.encode_credentials(tenant, proxy, credentials)
    assert b'private-password' not in blob and b'proxy-secret' not in blob
    assert vault.decode_credentials(tenant, proxy, blob) == credentials
    for other_tenant, other_proxy, key in [(uuid4(),proxy,b'x'*32),(tenant,uuid4(),b'x'*32),(tenant,proxy,b'y'*32)]:
        with pytest.raises(ValueError, match='cannot be decrypted'):
            ProxyVault(key, 'test-key').decode_credentials(other_tenant, other_proxy, blob)
    with pytest.raises(ValueError):
        vault.decode_credentials(tenant, proxy, b'unknown historical format')
