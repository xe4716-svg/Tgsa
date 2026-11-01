import base64
import secrets

print("JWT_SIGNING_SECRET=" + secrets.token_urlsafe(48))
print("SESSION_KEK_BASE64=" + base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
