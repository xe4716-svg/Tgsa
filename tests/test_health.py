from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.health import router


@pytest.fixture
def health_app():
    # Test probe behavior independently of production startup and its secrets.
    app = FastAPI()
    app.include_router(router)
    return app


def test_live_endpoint(health_app):
    # No database, Redis or deployment credentials are needed for liveness.
    with TestClient(health_app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


@pytest.mark.parametrize("postgres_ok,redis_ok", [
    (True, True), (False, True), (True, False), (False, False),
])
def test_readiness_reports_each_dependency(health_app, postgres_ok, redis_ok):
    query = AsyncMock(return_value=1)
    ping = AsyncMock(return_value=True)
    if not postgres_ok:
        query.side_effect = ConnectionError("database unavailable")
    if not redis_ok:
        ping.side_effect = ConnectionError("redis unavailable")

    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace(fetchval=query)

    health_app.state.db_pool = SimpleNamespace(acquire=acquire)
    health_app.state.redis = SimpleNamespace(ping=ping)
    with TestClient(health_app) as client:
        response = client.get("/health/ready")
    ok = postgres_ok and redis_ok
    assert response.status_code == (200 if ok else 503)
    assert response.json() == {
        "status": "ready" if ok else "not_ready",
        "checks": {"postgres": postgres_ok, "redis": redis_ok},
    }
    query.assert_awaited_once_with("SELECT 1")
    ping.assert_awaited_once()
