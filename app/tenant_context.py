import asyncpg
from uuid import UUID

async def set_tenant_context(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    # Call only after the authenticated user is verified as a tenant member.
    # Use inside an explicit transaction; never trust a client-supplied tenant_id alone.
    await conn.execute(
        "SELECT set_config('app.tenant_id', $1, true)",
        str(tenant_id),
    )
