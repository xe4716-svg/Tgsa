import asyncio
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.security import create_access_token
from app.config import settings
from app.main import app
from app.telegram.login_manager import PendingLogin

# No global client

TEST_USER_ID = uuid.uuid4()
TEST_TENANT_ID = uuid.uuid4()
TEST_FAMILY_ID = uuid.uuid4()

def get_auth_headers():
    token = create_access_token(
        user_id=str(TEST_USER_ID),
        tenant_id=str(TEST_TENANT_ID),
        family_id=str(TEST_FAMILY_ID),
        role="OWNER"
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def mock_db_and_lifespan():
    with patch("app.main.asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
        class MockConnection:
            async def __aenter__(self): return self
            async def __aexit__(self, exc_type, exc, tb): pass
            async def fetchrow(self, query, *args, **kwargs):
                if "proxy_profiles" in query:
                    return {
                        "id": args[1] if len(args) > 1 else uuid.uuid4(),
                        "proxy_type": "socks5",
                        "host": "localhost",
                        "port": 1080,
                        "username": "",
                        "password": "",
                        "secret": "",
                        "verification_status": "VERIFIED",
                        "status": "ACTIVE"
                    }
                if "users u" in query:
                    return {
                        "email": "test@example.com",
                        "status": "ACTIVE",
                        "platform_admin": False,
                        "role": "OWNER",
                        "tenant_status": "ACTIVE",
                        "session_active": True
                    }
                return None
            async def execute(self, *args, **kwargs): pass
            def transaction(self): return MockTransaction()
            async def fetchval(self, query, *args):
                if "COUNT(*)" in query:
                    return 0
                if "INSERT INTO telegram_accounts" in query:
                    return uuid.uuid4()
                return None

        class MockTransaction:
            async def __aenter__(self): pass
            async def __aexit__(self, exc_type, exc, tb): pass

        class MockPoolClass:
            def acquire(self):
                return MockConnection()
            async def close(self): pass

        mock_pool.return_value = MockPoolClass()
        import os
        from app.config import settings
        object.__setattr__(settings, "jwt_signing_secret", "test-only-secret-with-32-or-more-characters")
        object.__setattr__(settings, "session_kek_base64", "x"*43 + "=")
        object.__setattr__(settings, "session_kek_id", "test-key-v1")
        object.__setattr__(settings, "smtp_host", "localhost")

        from app.telegram.login_manager import TelegramLoginManager
        if not hasattr(app.state, "telegram_login_manager"):
            app.state.telegram_login_manager = TelegramLoginManager()
        yield


@pytest.mark.asyncio
async def test_unauthorized_access():
    with TestClient(app) as client:
        response = client.post("/telegram/login/qr", json={"connection_mode": "DIRECT"})
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_start_qr_success(monkeypatch):
    mock_start_qr = AsyncMock(return_value={"login_id": "test-id", "status": "QR_PENDING"})
    monkeypatch.setattr("app.telegram.login_manager.TelegramLoginManager.start_qr", mock_start_qr)

    headers = get_auth_headers()
    with TestClient(app) as client:
        response = client.post("/telegram/login/qr", json={"connection_mode": "DIRECT"}, headers=headers)
    print(response.json()); assert response.status_code == 200
    assert response.json() == {"login_id": "test-id", "status": "QR_PENDING"}


@pytest.mark.asyncio
async def test_consume_login_isolation_and_success(monkeypatch):
    login_id = "test-consume-id"
    pending = PendingLogin(
        login_id=login_id,
        tenant_id=str(TEST_TENANT_ID),
        user_id=str(TEST_USER_ID),
        method="QR",
        client=AsyncMock(),
        created_at=0,
        expires_at=9999999999,
        status="AUTHORIZED",
        session_string="mock-session-string",
        account={"telegram_user_id": 123},
        error_code=None,
        connection_mode="DIRECT",
        proxy_profile_id=None,
        lock=asyncio.Lock()
    )

    with TestClient(app) as client:
        # Now the lifespan has started and we have the real manager
        manager = client.app.state.telegram_login_manager
        manager._pending[login_id] = pending

        other_token = create_access_token(user_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()), family_id=str(uuid.uuid4()), role="OWNER")
        response = client.post(f"/telegram/login/{login_id}/consume", json={"notice_version": "v1.0"}, headers={"Authorization": f"Bearer {other_token}"})
        assert response.status_code == 404

        headers = get_auth_headers()
        with patch("app.telegram.router.persist_authorized_account", new_callable=AsyncMock) as mock_persist:
            mock_persist.return_value = uuid.uuid4()
            response = client.post(f"/telegram/login/{login_id}/consume", json={"notice_version": "v1.0"}, headers=headers)

        assert response.status_code == 200
        assert "account_id" in response.json()
        assert pending.status == "CONSUMED"

        response = client.post(f"/telegram/login/{login_id}/consume", json={"notice_version": "v1.0"}, headers=headers)
        assert response.status_code == 200
        assert "account_id" in response.json()

@pytest.mark.asyncio
async def test_consume_login_failure_recovery(monkeypatch):
    login_id = "test-fail-id"
    pending = PendingLogin(
        login_id=login_id,
        tenant_id=str(TEST_TENANT_ID),
        user_id=str(TEST_USER_ID),
        method="QR",
        client=AsyncMock(),
        created_at=0,
        expires_at=9999999999,
        status="AUTHORIZED",
        session_string="mock-session-string",
        account={"telegram_user_id": 123},
        error_code=None,
        connection_mode="DIRECT",
        proxy_profile_id=None,
        lock=asyncio.Lock()
    )

    with TestClient(app) as client:
        manager = client.app.state.telegram_login_manager
        manager._pending[login_id] = pending

        headers = get_auth_headers()
        with patch("app.telegram.router.persist_authorized_account", side_effect=ValueError("Limit reached")):
            response = client.post(f"/telegram/login/{login_id}/consume", json={"notice_version": "v1.0"}, headers=headers)

        assert response.status_code == 402
        assert pending.status == "AUTHORIZED" # Still authorized, can try again
        assert pending.session_string is not None

@pytest.mark.asyncio
async def test_shutdown_cleans_up_all_pending_tasks(monkeypatch):
    login_id = "test-shutdown-id"
    wait_task = asyncio.create_task(asyncio.sleep(10))
    pending = PendingLogin(
        login_id=login_id,
        tenant_id=str(TEST_TENANT_ID),
        user_id=str(TEST_USER_ID),
        method="QR",
        client=AsyncMock(),
        created_at=0,
        expires_at=9999999999,
        status="QR_PENDING",
        wait_task=wait_task,
        lock=asyncio.Lock()
    )

    with TestClient(app) as client:
        manager = client.app.state.telegram_login_manager
        manager._pending[login_id] = pending

        # Test shutdown
        await manager.shutdown()
        await asyncio.sleep(0.01)

        assert len(manager._pending) == 0
        assert pending.wait_task.done() or pending.wait_task.cancelled()
        pending.client.disconnect.assert_called_once()

@pytest.mark.asyncio
async def test_resolve_proxy_profile_rls_and_isolation(monkeypatch):
    headers = get_auth_headers()
    proxy_id = str(uuid.uuid4())

    # 1. DIRECT mode shouldn't query the proxy profile DB
    with patch("app.telegram.login_manager.create_client") as mock_create_client:
        mock_create_client.return_value = AsyncMock()
        mock_create_client.return_value.qr_login.return_value = AsyncMock(url="test_url")
        with TestClient(app) as client:
            response = client.post("/telegram/login/qr", json={"connection_mode": "DIRECT"}, headers=headers)
        assert response.status_code == 200

    # 2. PROXY mode with an unverified/inactive proxy throws 400
    with patch("app.main.asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
        class MockConnectionUnverified:
            async def __aenter__(self): return self
            async def __aexit__(self, exc_type, exc, tb): pass
            async def execute(self, *args, **kwargs): pass
            def transaction(self): return self
            async def fetchrow(self, query, *args, **kwargs):
                if "proxy_profiles" in query:
                    return {
                        "id": args[1],
                        "proxy_type": "socks5",
                        "host": "localhost",
                        "port": 1080,
                        "username": "",
                        "password": "",
                        "secret": "",
                        "verification_status": "UNVERIFIED",
                        "status": "ACTIVE"
                    }
                if "users u" in query:
                    return {"email": "test@example.com", "status": "ACTIVE", "platform_admin": False, "role": "OWNER", "tenant_status": "ACTIVE", "session_active": True}
                return None
        class MockPoolClassUnverified:
            def acquire(self): return MockConnectionUnverified()
            async def close(self): pass

        mock_pool.return_value = MockPoolClassUnverified()

        with TestClient(app) as client:
            response = client.post("/telegram/login/qr", json={"connection_mode": "PROXY", "proxy_profile_id": proxy_id}, headers=headers)
        assert response.status_code == 400
        assert "verified and active" in response.json()["detail"].lower()
