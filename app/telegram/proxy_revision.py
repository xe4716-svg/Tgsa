"""Fingerprint the verified database profile without exposing its credentials."""
import hashlib
import json


def proxy_revision(profile):
    fields = ('id','proxy_type','host','port','credentials_ciphertext','secret_version','verification_status','status')
    values = {key: profile.get(key) for key in fields}
    blob = values['credentials_ciphertext']
    if blob is not None:
        values['credentials_ciphertext'] = bytes(blob).hex()
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()
