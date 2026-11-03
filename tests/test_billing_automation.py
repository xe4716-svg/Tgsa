from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport

from app.billing.automation import (
    _format_amount,
    _validate_timezone,
    reminder_bucket,
)
from app.auth.dependencies import AuthPrincipal
from app.main import app


def test_reminder_bucket_uses_one_day_windows():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert reminder_bucket(now + timedelta(days=6, hours=12), now, [7, 3, 1, 0]) == 7
    assert reminder_bucket(now + timedelta(days=2, hours=12), now, [7, 3, 1, 0]) == 3
    assert reminder_bucket(now + timedelta(hours=12), now, [7, 3, 1, 0]) == 1
    assert reminder_bucket(now - timedelta(hours=2), now, [7, 3, 1, 0]) == 0


def test_reminder_bucket_does_not_repeat_outside_configured_windows():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert reminder_bucket(now + timedelta(days=5), now, [7, 3, 1, 0]) is None
    assert reminder_bucket(now - timedelta(days=2), now, [7, 3, 1, 0]) is None


def test_timezone_must_be_a_valid_iana_zone():
    assert _validate_timezone("America/Los_Angeles") == "America/Los_Angeles"
    with pytest.raises(HTTPException) as error:
        _validate_timezone("Mars/Olympus_Mons")
    assert error.value.status_code == 422


def test_currency_amounts_use_integer_minor_units():
    assert _format_amount("CNY", 12345) == "CNY 123.45"
    assert _format_amount("USD", 999) == "USD 9.99"
    assert _format_amount("USDT", 1234567) == "USDT 1.234567"


@pytest.fixture
def mock_principal_dependency():
    from app.auth.dependencies import get_current_principal

    class PrincipalMock:
        def __init__(self):
            self.principal = None

        def set_principal(self, user_id, tenant_id):
            self.principal = AuthPrincipal(
                user_id=user_id,
                tenant_id=tenant_id,
                family_id=uuid4(),
                role="OWNER",
                email="test@example.com",
                platform_admin=False,
            )

        async def __call__(self):
            if not self.principal:
                raise HTTPException(status_code=401, detail="Authentication required")
            return self.principal

    mock = PrincipalMock()
    app.dependency_overrides[get_current_principal] = mock
    yield mock
    app.dependency_overrides.pop(get_current_principal, None)


@pytest.mark.anyio
async def test_unread_count_and_read_all_isolation_and_idempotency(mock_principal_dependency):
    try:
        from app.config import settings
        import asyncpg
        pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=1)
    except Exception as e:
        pytest.skip(f"Test requires real database connection. ({e})")

    try:
        app.state.db_pool = pool

        tenant_1 = uuid4()
        tenant_2 = uuid4()
        user_1 = uuid4()
        user_2 = uuid4()

        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO tenants (id, name, status) VALUES ($1, 'T1', 'ACTIVE'), ($2, 'T2', 'ACTIVE')", tenant_1, tenant_2)
            await conn.execute(
                """
                INSERT INTO users (id, auth_subject, email, status)
                VALUES
                    ($1, 'ci-billing-user-1', 'u1@t.com', 'ACTIVE'),
                    ($2, 'ci-billing-user-2', 'u2@t.com', 'ACTIVE')
                """,
                user_1,
                user_2,
            )
            await conn.execute("INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, 'OWNER')", tenant_1, user_1)
            await conn.execute("INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, 'MEMBER')", tenant_1, user_2)
            await conn.execute("INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES ($1, $2, 'OWNER')", tenant_2, user_1)

            await conn.execute("""
                INSERT INTO tenant_notifications (tenant_id, user_id, dedupe_key, event_type, title, body)
                VALUES
                ($1, $2, 't1-u1-1', 'E', 'T', 'B'),
                ($1, $2, 't1-u1-2', 'E', 'T', 'B'),
                ($1, $2, 't1-u1-3', 'E', 'T', 'B'),
                ($1, $3, 't1-u2-1', 'E', 'T', 'B'),
                ($1, $3, 't1-u2-2', 'E', 'T', 'B'),
                ($4, $2, 't2-u1-1', 'E', 'T', 'B')
            """, tenant_1, user_1, user_2, tenant_2)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Unauthenticated request
            resp = await client.get("/billing/notifications/unread-count")
            assert resp.status_code == 401

            resp = await client.post("/billing/notifications/read-all")
            assert resp.status_code == 401

            # 2. Test unread count for user 1 in tenant 1
            mock_principal_dependency.set_principal(user_1, tenant_1)
            resp = await client.get("/billing/notifications/unread-count")
            assert resp.status_code == 200
            assert resp.json() == {"unread_count": 3}

            # 3. Read all for user 1 in tenant 1
            resp = await client.post("/billing/notifications/read-all")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert resp.json()["updated_count"] == 3

            # 4. Verify count is now 0
            resp = await client.get("/billing/notifications/unread-count")
            assert resp.status_code == 200
            assert resp.json() == {"unread_count": 0}

            # 5. Idempotency check: Read all again
            resp = await client.post("/billing/notifications/read-all")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert resp.json()["updated_count"] == 0

            # 6. Verify Isolation: Other users/tenants unaffected
            mock_principal_dependency.set_principal(user_2, tenant_1)
            resp = await client.get("/billing/notifications/unread-count")
            assert resp.status_code == 200
            assert resp.json() == {"unread_count": 2}

            mock_principal_dependency.set_principal(user_1, tenant_2)
            resp = await client.get("/billing/notifications/unread-count")
            assert resp.status_code == 200
            assert resp.json() == {"unread_count": 1}

            # Check db explicitly to ensure historical read_at is preserved across idempotency
            async with pool.acquire() as conn:
                t1_u1_notifs = await conn.fetch("SELECT read_at FROM tenant_notifications WHERE tenant_id = $1 AND user_id = $2", tenant_1, user_1)
                assert all(n["read_at"] is not None for n in t1_u1_notifs)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tenant_notifications WHERE tenant_id IN ($1, $2)", tenant_1, tenant_2)
            await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id IN ($1, $2)", tenant_1, tenant_2)
            await conn.execute("DELETE FROM users WHERE id IN ($1, $2)", user_1, user_2)
            await conn.execute("DELETE FROM tenants WHERE id IN ($1, $2)", tenant_1, tenant_2)

        await pool.close()
        if hasattr(app.state, "db_pool") and app.state.db_pool == pool:
            del app.state.db_pool
