import pytest
from app.telegram.client_factory import build_proxy, validate_connection_choice

VERIFIED_SOCKS = {
    "proxy_type": "SOCKS5",
    "host": "proxy.example.net",
    "port": 1080,
    "username": "user",
    "password": "secret",
    "verification_status": "VERIFIED",
    "status": "ACTIVE",
}


def test_socks5_proxy_uses_async_proxy_config():
    proxy, connection = build_proxy(VERIFIED_SOCKS)
    assert proxy["proxy_type"] == "socks5"
    assert proxy["addr"] == "proxy.example.net"
    assert proxy["port"] == 1080
    assert proxy["rdns"] is True
    assert connection is None


def test_proxy_rejects_invalid_port():
    profile = {**VERIFIED_SOCKS, "port": 70000}
    with pytest.raises(ValueError):
        build_proxy(profile)


def test_proxy_requires_verified_state():
    profile = {**VERIFIED_SOCKS, "verification_status": "UNVERIFIED"}
    with pytest.raises(ValueError):
        build_proxy(profile)


def test_http_connect_is_not_silently_enabled():
    profile = {**VERIFIED_SOCKS, "proxy_type": "HTTP_CONNECT"}
    with pytest.raises(NotImplementedError):
        build_proxy(profile)


def test_connection_choice_must_be_explicit():
    with pytest.raises(ValueError):
        validate_connection_choice("PROXY", None)
    with pytest.raises(ValueError):
        validate_connection_choice("DIRECT", VERIFIED_SOCKS)
    validate_connection_choice("DIRECT", None)


def test_managed_client_constructor_accepts_installed_telethon_contract(monkeypatch):
    from types import SimpleNamespace
    from app.telegram import client_factory
    monkeypatch.setattr(client_factory,'settings',SimpleNamespace(telegram_api_id=123,telegram_api_hash='s'*32))
    client=client_factory.create_client(connection_mode='DIRECT',managed=True)
    assert not client.is_connected()
    assert client.flood_sleep_threshold==0
