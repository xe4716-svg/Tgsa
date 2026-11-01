import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("redis")
pytest.importorskip("aiosmtplib")

from fastapi.testclient import TestClient
from app.main import app


def test_live_endpoint():
    # Readiness requires live PostgreSQL and Redis; liveness does not.
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"
