import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "TG SaaS API")
    app_env: str = os.getenv("APP_ENV", "development")
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://tgs_runtime:dev-only-change-me@localhost:5432/tgs",
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    telegram_api_id: int | None = (
        int(os.environ["TG_API_ID"]) if os.getenv("TG_API_ID") else None
    )
    telegram_api_hash: str | None = os.getenv("TG_API_HASH")
    session_kek_base64: str | None = os.getenv("SESSION_KEK_BASE64")
    session_kek_id: str = os.getenv("SESSION_KEK_ID", "local-dev-key-v1")

    jwt_signing_secret: str | None = os.getenv("JWT_SIGNING_SECRET")
    access_token_ttl_seconds: int = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900"))
    refresh_token_ttl_days: int = int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "30"))

    smtp_host: str | None = os.getenv("SMTP_HOST")
    smtp_port: int = int(os.getenv("SMTP_PORT", "1025"))
    smtp_username: str | None = os.getenv("SMTP_USERNAME")
    smtp_password: str | None = os.getenv("SMTP_PASSWORD")
    smtp_starttls: bool = _env_bool("SMTP_STARTTLS", False)
    smtp_use_tls: bool = _env_bool("SMTP_USE_TLS", False)
    email_from: str = os.getenv("EMAIL_FROM", "no-reply@tgs.local")
    email_verification_url: str = os.getenv(
        "EMAIL_VERIFICATION_URL", "http://localhost:3000/verify-email"
    )
    password_reset_url: str = os.getenv(
        "PASSWORD_RESET_URL", "http://localhost:3000/reset-password"
    )


settings = Settings()



def validate_startup_settings() -> None:
    import base64

    secret = settings.jwt_signing_secret
    if not secret or len(secret.encode("utf-8")) < 32 or "replace-with" in secret:
        raise RuntimeError(
            "Set JWT_SIGNING_SECRET to a unique random value of at least 32 characters"
        )

    encoded_key = settings.session_kek_base64
    if not encoded_key:
        raise RuntimeError("SESSION_KEK_BASE64 must contain a base64-encoded 32-byte key")
    try:
        session_key = base64.b64decode(encoded_key, validate=True)
    except Exception as exc:
        raise RuntimeError("SESSION_KEK_BASE64 is not valid base64") from exc
    if len(session_key) != 32:
        raise RuntimeError("SESSION_KEK_BASE64 must decode to exactly 32 bytes")

    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST must be configured before starting the API")

    if settings.app_env.lower() == "production":
        if not (settings.smtp_starttls or settings.smtp_use_tls):
            raise RuntimeError("Production SMTP must use TLS")
        if not settings.email_verification_url.startswith("https://"):
            raise RuntimeError("Production email verification URL must use HTTPS")
        if not settings.password_reset_url.startswith("https://"):
            raise RuntimeError("Production password reset URL must use HTTPS")
