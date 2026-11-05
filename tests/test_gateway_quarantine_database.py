"""Real disposable-PostgreSQL regression for FloodWait + uncertain disconnect.

This test creates and drops its own child database. It never connects to a
persistent application database or a Telegram network endpoint.
"""
import asyncio
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

import asyncpg
import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from telethon.errors import FloodWaitError

from app.auth.dependencies import AuthPrincipal
from app.telegram.gateway_leases import GatewayLeases, LeaseLost
from app.telegram.gateway_material import AccountMaterial
from app.telegram.gateway_routes import GatewayRoutes, RouteConflict
from app.telegram.gateway_runtime import GatewayRunner, GatewayCloseUncertain


@pytest.mark.skipif(
    os.getenv("TGMATE_GATEWAY_INTEGRATION") != "1",
    reason="disposable database opt-in required",
)
def test_flood_wait_then_disconnect_failure_is_atomically_quarantined():
    source_database = os.getenv("PGDATABASE", "")
    if source_database != "tgmate_ci" and not source_database.startswith("tgs_migration_"):
        pytest.fail("Explicit disposable CI or migration database required")

    root = Path(__file__).resolve().parents[1]
    psql = os.getenv("PG_PSQL", "psql")
    database = "tgs_gateway_" + uuid4().hex[:12]
    config = dict(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname="postgres",
        connect_timeout=5,
    )
    env = dict(
        os.environ,
        PGDATABASE=database,
        PGCLIENTENCODING="UTF8",
        APP_DB_PASSWORD=secrets.token_urlsafe(32),
    )
    tenant_id, user_id, account_id, family_id = [uuid4() for _ in range(4)]
    email = f"{user_id}@example.test"

    with psycopg.connect(**config, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            migrated = subprocess.run(
                [sys.executable, str(root / "scripts" / "migrate.py"), "--psql", psql],
                env=env,
                capture_output=True,
                text=True,
            )
            assert migrated.returncode == 0, "Root migrations failed in disposable gateway database"

            child_config = dict(config, dbname=database)
            with psycopg.connect(**child_config, autocommit=True) as owner:
                owner.execute(
                    "INSERT INTO tenants(id,name) VALUES (%s,'gateway integration')",
                    (tenant_id,),
                )
                owner.execute(
                    "INSERT INTO users(id,auth_subject,email,status) VALUES (%s,%s,%s,'ACTIVE')",
                    (user_id, str(user_id), email),
                )
                owner.execute(
                    "INSERT INTO tenant_memberships(tenant_id,user_id,role) VALUES (%s,%s,'OWNER')",
                    (tenant_id, user_id),
                )
                owner.execute(
                    """INSERT INTO auth_refresh_tokens(token_hash,tenant_id,user_id,family_id,expires_at)
                       VALUES (%s,%s,%s,%s,clock_timestamp()+interval '1 day')""",
                    ("a" * 64, tenant_id, user_id, family_id),
                )
                owner.execute(
                    """INSERT INTO telegram_accounts(id,tenant_id,status,security_state,connection_mode)
                       VALUES (%s,%s,'AUTHORIZED','CLEAR','DIRECT')""",
                    (account_id, tenant_id),
                )
                owner.execute(
                    """INSERT INTO account_gateway_routes(tenant_id,account_id,enabled,generation)
                       VALUES (%s,%s,true,1)""",
                    (tenant_id, account_id),
                )

            async def exercise():
                pool = await asyncpg.create_pool(
                    host=config["host"],
                    port=config["port"],
                    database=database,
                    user=config["user"],
                    password=config["password"],
                    min_size=1,
                    max_size=2,
                )

                class RecordingLeases(GatewayLeases):
                    before_defer = None
                    after_defer = None

                    async def defer(self, lease, seconds):
                        self.before_defer = lease
                        updated = await super().defer(lease, seconds)
                        self.after_defer = updated
                        if updated is not None:
                            with pytest.raises(LeaseLost):
                                async with self.fenced_transaction(lease):
                                    pass
                        return updated

                class FaultInjectedClient:
                    def __init__(self):
                        self.disconnected = asyncio.get_running_loop().create_future()

                    async def connect(self):
                        return None

                    async def is_user_authorized(self):
                        return True

                    async def get_me(self):
                        raise FloodWaitError(request=None, capture=1)

                    async def disconnect(self):
                        raise OSError("injected disconnect uncertainty")

                client = FaultInjectedClient()
                leases = RecordingLeases(pool)

                async def loader(conn, lease, vault, *, decrypt):
                    if decrypt:
                        return AccountMaterial(123, "DIRECT", "revision-1", "test-session", None)
                    return "revision-1"

                runner = GatewayRunner(
                    pool,
                    object(),
                    "gateway-integration-test",
                    ttl=6,
                    heartbeat=0.01,
                    control_timeout=0.5,
                    rpc_timeout=0.3,
                    close_timeout=0.2,
                    leases=leases,
                    loader=loader,
                    client_factory=lambda *args, **kwargs: client,
                )

                try:
                    with pytest.raises(GatewayCloseUncertain):
                        await runner.run(
                            tenant_id,
                            account_id,
                            asyncio.Event(),
                            route_generation=1,
                        )

                    assert leases.before_defer is not None
                    assert leases.after_defer is not None
                    assert leases.after_defer.fencing_token == leases.before_defer.fencing_token + 1

                    async with pool.acquire() as conn:
                        account = await conn.fetchrow(
                            "SELECT status,security_state FROM telegram_accounts WHERE id=$1",
                            account_id,
                        )
                        lease_row = await conn.fetchrow(
                            """SELECT fencing_token,lease_expires_at>clock_timestamp() AS live
                               FROM account_gateway_leases WHERE account_id=$1""",
                            account_id,
                        )
                        route = await conn.fetchrow(
                            """SELECT enabled,generation,last_reason
                               FROM account_gateway_routes WHERE account_id=$1""",
                            account_id,
                        )
                        events = await conn.fetch(
                            "SELECT event_type,severity FROM account_security_events WHERE account_id=$1",
                            account_id,
                        )
                        audits = await conn.fetch(
                            "SELECT action FROM audit_logs WHERE resource_id=$1",
                            account_id,
                        )

                    assert account["status"] == "PAUSED"
                    assert account["security_state"] == "GATEWAY_CLOSE_UNCERTAIN"
                    assert lease_row["fencing_token"] == leases.after_defer.fencing_token
                    assert lease_row["live"] is True
                    assert route["enabled"] is False
                    assert route["generation"] == 2
                    assert route["last_reason"] == "DISCONNECT_UNCERTAIN"
                    assert {(row["event_type"], row["severity"]) for row in events} >= {
                        ("MTPROTO_FLOOD_WAIT", "WARNING"),
                        ("GATEWAY_CLOSE_UNCERTAIN", "CRITICAL"),
                    }
                    assert {row["action"] for row in audits} >= {
                        "GATEWAY_SERVER_WAIT",
                        "GATEWAY_DISCONNECT_UNCERTAIN",
                        "GATEWAY_ROUTE_PAUSED",
                    }

                    # Even if account status is subsequently reset by an
                    # external recovery attempt, the durable route marker blocks
                    # re-enabling until the explicit recovery flow clears it.
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """UPDATE telegram_accounts
                               SET status='AUTHORIZED',security_state='CLEAR' WHERE id=$1""",
                            account_id,
                        )
                    principal = AuthPrincipal(
                        user_id, tenant_id, family_id, "OWNER", email, False
                    )
                    routes = GatewayRoutes(pool)
                    disabled = await routes.change(
                        principal, account_id, False, expected_generation=2
                    )
                    assert disabled["enabled"] is False
                    assert disabled["generation"] == 3
                    assert disabled["last_reason"] == "DISCONNECT_UNCERTAIN"
                    with pytest.raises(RouteConflict, match="explicit recovery"):
                        await routes.change(
                            principal, account_id, True, expected_generation=3
                        )
                finally:
                    await pool.close()

            asyncio.run(exercise())
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
