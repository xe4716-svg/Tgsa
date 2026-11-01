from uuid import uuid4

import pytest

from app.security.session_vault import SessionVault
from app.security.session_bundle import (
    SessionBundleAccount, export_bundle, import_bundle
)


def test_session_vault_round_trip_and_tenant_binding():
    tenant_id, account_id = uuid4(), uuid4()
    vault = SessionVault(b"x" * 32, "test-key-v1")
    encrypted = vault.encrypt(tenant_id, account_id, "telethon-session-secret")
    assert vault.decrypt(tenant_id, account_id, encrypted) == "telethon-session-secret"

    with pytest.raises(Exception):
        vault.decrypt(uuid4(), account_id, encrypted)


def test_session_bundle_multi_account_round_trip():
    accounts = [
        SessionBundleAccount(session="session-one", username="first"),
        SessionBundleAccount(session="session-two", phone_masked="+1******1234"),
    ]
    blob = export_bundle(accounts, "a-long-test-password-123")
    restored = import_bundle(blob, "a-long-test-password-123")
    assert len(restored) == 2
    assert restored[0].session == "session-one"
    assert restored[1].phone_masked == "+1******1234"


def test_session_bundle_rejects_wrong_password():
    blob = export_bundle(
        [SessionBundleAccount(session="session-one")],
        "a-long-test-password-123",
    )
    with pytest.raises(ValueError):
        import_bundle(blob, "a-different-password-123")


def test_session_bundle_requires_strong_password():
    with pytest.raises(ValueError):
        export_bundle([SessionBundleAccount(session="secret")], "weak")
