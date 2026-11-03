"""Load encrypted account material under a fenced, tenant-scoped transaction."""
import hashlib
import json
from dataclasses import dataclass, field

from app.security.proxy_vault import ProxyVault
from app.security.session_vault import EncryptedSession
from app.telegram.client_factory import validate_connection_choice
from app.telegram.proxy_revision import proxy_revision


@dataclass(frozen=True)
class AccountMaterial:
    telegram_user_id: int
    connection_mode: str
    revision: str
    session_string: str = field(repr=False)
    proxy_profile: dict | None = field(repr=False)


async def load_material(conn, lease, vault, *, decrypt=True):
    row = await conn.fetchrow('''SELECT a.telegram_user_id,a.connection_mode,a.security_state,
             s.ciphertext,s.nonce,s.wrapped_dek,s.kms_key_id,s.encryption_version
          FROM telegram_accounts a JOIN telegram_sessions s
            ON s.tenant_id=a.tenant_id AND s.account_id=a.id
          WHERE a.tenant_id=$1 AND a.id=$2''',lease.tenant_id,lease.account_id)
    if not row or type(row['telegram_user_id']) is not int or row['telegram_user_id']<=0:
        raise ValueError('Authorized account material is missing')
    bindings=await conn.fetch('''SELECT b.id AS binding_id,p.id,p.proxy_type,p.host,p.port,
             p.credentials_ciphertext,p.secret_version,p.verification_status,p.status
          FROM account_proxy_bindings b JOIN proxy_profiles p
            ON p.tenant_id=b.tenant_id AND p.id=b.proxy_profile_id
          WHERE b.tenant_id=$1 AND b.account_id=$2 AND b.is_active''',lease.tenant_id,lease.account_id)
    mode=row['connection_mode']
    if mode=='PROXY' and len(bindings)!=1 or mode=='DIRECT' and bindings:
        raise ValueError('Account network binding is inconsistent')
    profile=dict(bindings[0]) if bindings else None
    if profile and (profile['verification_status']!='VERIFIED' or profile['status']!='ACTIVE'):
        raise ValueError('Proxy is not verified and active')
    state={key:(bytes(row[key]).hex() if key in ('ciphertext','nonce','wrapped_dek') else row[key])
           for key in row.keys()}
    state['proxy_revision']=proxy_revision(profile) if profile else None
    state['binding_id']=str(profile['binding_id']) if profile else None
    revision=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()
    if not decrypt:
        return revision
    if profile:
        ciphertext=profile.pop('credentials_ciphertext')
        if ciphertext:
            proxy_vault=ProxyVault(vault._master_key,vault.key_id)
            profile.update(proxy_vault.decode_credentials(lease.tenant_id,profile['id'],ciphertext))
    validate_connection_choice(mode,profile)
    record=EncryptedSession(**{key:row[key] for key in ('ciphertext','nonce','wrapped_dek','kms_key_id','encryption_version')})
    session=vault.decrypt(lease.tenant_id,lease.account_id,record)
    return AccountMaterial(row['telegram_user_id'],mode,revision,session,profile)
