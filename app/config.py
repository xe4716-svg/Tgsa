import os
import re
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    run_background_workers: bool = _env_bool('RUN_BACKGROUND_WORKERS', False)
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

    platform_telegram_bot_token: str | None = os.getenv("PLATFORM_TELEGRAM_BOT_TOKEN")
    platform_telegram_bot_username: str | None = os.getenv("PLATFORM_TELEGRAM_BOT_USERNAME")
    platform_telegram_webhook_secret: str | None = os.getenv("PLATFORM_TELEGRAM_WEBHOOK_SECRET")

    # Payment provider credentials are optional until a provider is enabled.
    public_api_base_url: str = os.getenv("PUBLIC_API_BASE_URL", "http://localhost:8000")
    payment_return_url: str = os.getenv("PAYMENT_RETURN_URL", "http://localhost:3000/wallet")
    manual_payment_contact_url: str = os.getenv("MANUAL_PAYMENT_CONTACT_URL", "")

    stripe_secret_key: str | None = os.getenv("STRIPE_SECRET_KEY")
    stripe_webhook_secret: str | None = os.getenv("STRIPE_WEBHOOK_SECRET")

    alipay_app_id: str | None = os.getenv("ALIPAY_APP_ID")
    alipay_private_key_pem: str | None = os.getenv("ALIPAY_PRIVATE_KEY_PEM")
    alipay_public_key_pem: str | None = os.getenv("ALIPAY_PUBLIC_KEY_PEM")
    alipay_gateway_url: str = os.getenv(
        "ALIPAY_GATEWAY_URL", "https://openapi.alipay.com/gateway.do"
    )

    wechat_mchid: str | None = os.getenv("WECHAT_MCHID")
    wechat_appid: str | None = os.getenv("WECHAT_APPID")
    wechat_serial_no: str | None = os.getenv("WECHAT_SERIAL_NO")
    wechat_private_key_pem: str | None = os.getenv("WECHAT_PRIVATE_KEY_PEM")
    wechat_api_v3_key: str | None = os.getenv("WECHAT_API_V3_KEY")
    wechat_platform_cert_pem: str | None = os.getenv("WECHAT_PLATFORM_CERT_PEM")
    wechat_api_base_url: str = os.getenv(
        "WECHAT_API_BASE_URL", "https://api.mch.weixin.qq.com"
    )

    easypay_gateway_url: str | None = os.getenv("EASYPAY_GATEWAY_URL")
    easypay_pid: str | None = os.getenv("EASYPAY_PID")
    easypay_key: str | None = os.getenv("EASYPAY_KEY")

    tron_api_base_url: str = os.getenv("TRON_API_BASE_URL", "https://api.trongrid.io")
    trongrid_api_key: str | None = os.getenv("TRONGRID_API_KEY")
    tron_usdt_contract: str = os.getenv(
        "TRON_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    )
    tron_account_xpub: str | None = os.getenv("TRON_ACCOUNT_XPUB")
    tron_watcher_enabled: bool = _env_bool("TRON_WATCHER_ENABLED", False)
    tron_watch_interval_seconds: int = int(os.getenv("TRON_WATCH_INTERVAL_SECONDS", "60"))
    tron_watch_batch_size: int = int(os.getenv("TRON_WATCH_BATCH_SIZE", "20"))
    tron_sweep_enabled: bool = _env_bool("TRON_SWEEP_ENABLED", False)
    tron_sweep_signer_url: str | None = os.getenv("TRON_SWEEP_SIGNER_URL")
    tron_sweep_signer_token: str | None = os.getenv("TRON_SWEEP_SIGNER_TOKEN")
    tron_treasury_address: str | None = os.getenv("TRON_TREASURY_ADDRESS")
    tron_sweep_threshold_minor: int = int(os.getenv("TRON_SWEEP_THRESHOLD_MINOR", "10000000"))
    tron_sweep_max_fee_sun: int = int(os.getenv("TRON_SWEEP_MAX_FEE_SUN", "0"))


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

    telegram_values = (
        settings.platform_telegram_bot_token,
        settings.platform_telegram_bot_username,
        settings.platform_telegram_webhook_secret,
    )
    if any(telegram_values) and not all(telegram_values):
        raise RuntimeError(
            "Configure PLATFORM_TELEGRAM_BOT_TOKEN, PLATFORM_TELEGRAM_BOT_USERNAME and "
            "PLATFORM_TELEGRAM_WEBHOOK_SECRET together"
        )
    if all(telegram_values):
        bot_username = settings.platform_telegram_bot_username.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username):
            raise RuntimeError("PLATFORM_TELEGRAM_BOT_USERNAME must be a valid bot username")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", settings.platform_telegram_webhook_secret):
            raise RuntimeError("PLATFORM_TELEGRAM_WEBHOOK_SECRET must be 32-256 safe characters")

    if settings.app_env.lower() == "production":
        if not (settings.smtp_starttls or settings.smtp_use_tls):
            raise RuntimeError("Production SMTP must use TLS")
        if not settings.email_verification_url.startswith("https://"):
            raise RuntimeError("Production email verification URL must use HTTPS")
        if not settings.password_reset_url.startswith("https://"):
            raise RuntimeError("Production password reset URL must use HTTPS")
