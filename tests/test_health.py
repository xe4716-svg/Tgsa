
import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("redis")
pytest.importorskip("aiosmtplib")

from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture(autouse=True)
def mock_db_pool():
    with patch("app.main.asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
        class MockPoolClass:
            def acquire(self): return AsyncMock()
            async def close(self): pass
        mock_pool.return_value = MockPoolClass()
        yield



def test_live_endpoint():
    from app.config import settings
    object.__setattr__(settings, "jwt_signing_secret", "test-only-secret-with-32-or-more-characters")
    object.__setattr__(settings, "session_kek_base64", "x"*43 + "=")
    object.__setattr__(settings, "session_kek_id", "test-key-v1")
    object.__setattr__(settings, "smtp_host", "localhost")
    # Readiness requires live PostgreSQL and Redis; liveness does not.
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"
