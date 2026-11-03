from __future__ import annotations

from typing import Any, Literal

from app.config import settings

ConnectionMode = Literal["PROXY", "DIRECT"]


def _require_api_credentials() -> tuple[int, str]:
    if settings.telegram_api_id is None or not settings.telegram_api_hash:
        raise RuntimeError("TG_API_ID and TG_API_HASH must be configured in the secret manager")
    return settings.telegram_api_id, settings.telegram_api_hash


def build_proxy(proxy_profile: dict[str, Any] | None):
    """Build a Telethon proxy config. Never silently fall back to direct mode."""
    if not proxy_profile:
        raise ValueError("A verified proxy profile is required")

    proxy_type = str(proxy_profile.get("proxy_type", "")).upper()
    host = proxy_profile.get("host")
    port = proxy_profile.get("port")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Invalid proxy host")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid proxy port")
    if proxy_profile.get("verification_status") != "VERIFIED":
        raise ValueError("Proxy must pass connection testing before use")
    if proxy_profile.get("status", "ACTIVE") != "ACTIVE":
        raise ValueError("Proxy profile is disabled")

    if proxy_type == "SOCKS5":
        return {
            "proxy_type": "socks5",
            "addr": host,
            "port": port,
            "username": proxy_profile.get("username"),
            "password": proxy_profile.get("password"),
            "rdns": True,
        }, None

    if proxy_type == "MTPROTO":
        secret = proxy_profile.get("secret")
        if not isinstance(secret, str) or not secret:
            raise ValueError("MTProto proxy secret is required")
        from telethon import connection
        return (
            (host, port, secret),
            connection.ConnectionTcpMTProxyRandomizedIntermediate,
        )

    if proxy_type == "HTTP_CONNECT":
        raise NotImplementedError(
            "HTTP CONNECT is disabled until its transport adapter passes end-to-end tests"
        )

    raise ValueError("Unsupported proxy type")


def validate_connection_choice(
    connection_mode: str,
    proxy_profile: dict[str, Any] | None,
) -> None:
    mode = str(connection_mode).upper()
    if mode == "PROXY":
        if proxy_profile is None:
            raise ValueError("Choose a tested proxy before selecting proxy mode")
        build_proxy(proxy_profile)
        return
    if mode == "DIRECT":
        if proxy_profile is not None:
            raise ValueError("Direct mode cannot include a proxy profile")
        return
    raise ValueError("Connection mode must be PROXY or DIRECT")


def create_client(
    session_string: str = "",
    *,
    connection_mode: ConnectionMode,
    proxy_profile: dict[str, Any] | None = None,
    managed: bool = False,
):
    """Create a Telethon client with an explicit, user-selected route."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    connection_mode = str(connection_mode).upper()
    _require_api_credentials()
    validate_connection_choice(connection_mode, proxy_profile)

    proxy = None
    connection = None
    if connection_mode == "PROXY":
        proxy, connection = build_proxy(proxy_profile)

    kwargs: dict[str, Any] = {
        "proxy": proxy,
        "connection_retries": 0 if managed else 3,
        "request_retries": 0 if managed else 3,
        "auto_reconnect": not managed,
        "receive_updates": not managed,
        "flood_sleep_threshold": 0,
        "retry_delay": 2,
        "timeout": 10,
        "sequential_updates": True,
    }
    if connection is not None:
        kwargs["connection"] = connection

    return TelegramClient(
        StringSession(session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        **kwargs,
    )
