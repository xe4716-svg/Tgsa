import time
from types import SimpleNamespace

import pytest

from app.telegram.login_manager import PendingLogin, TelegramLoginManager


def test_login_flow_is_bound_to_tenant_and_user():
    manager = TelegramLoginManager()
    pending = PendingLogin(
        login_id="login-opaque-token",
        tenant_id="tenant-a",
        user_id="user-a",
        method="PHONE",
        client=SimpleNamespace(),
        created_at=time.time(),
        expires_at=time.time() + 60,
        status="CODE_REQUIRED",
    )
    manager._pending[pending.login_id] = pending

    assert manager.get_status("login-opaque-token", "tenant-a", "user-a")["status"] == "CODE_REQUIRED"
    with pytest.raises(PermissionError):
        manager.get_status("login-opaque-token", "tenant-b", "user-a")
    with pytest.raises(PermissionError):
        manager.get_status("login-opaque-token", "tenant-a", "user-b")


def test_login_flow_expires():
    manager = TelegramLoginManager()
    pending = PendingLogin(
        login_id="expired-token",
        tenant_id="tenant-a",
        user_id="user-a",
        method="PHONE",
        client=SimpleNamespace(),
        created_at=time.time() - 10,
        expires_at=time.time() - 1,
        status="CODE_REQUIRED",
    )
    manager._pending[pending.login_id] = pending

    with pytest.raises(TimeoutError):
        manager.get_status("expired-token", "tenant-a", "user-a")
