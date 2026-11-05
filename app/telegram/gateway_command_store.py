"""Narrow PostgreSQL command store for Gateway-owned account operations.

This module never loads Telegram session material and never creates a Telethon
client. The caller must already own the account Gateway lease.
"""
from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class GatewayCommand:
    id: UUID
    tenant_id: UUID
    account_id: UUID
    route_generation: int
    operation_key: UUID
    operation_kind: str
    destination_peer_id: int
    payload: dict[str, Any]
    claim_worker_id: str
    claim_fencing_token: int
    claim_until: Any


@dataclass(frozen=True)
class GatewayCommandResult:
    id: UUID
    account_id: UUID
    status: str
    result: dict[str, Any] | None
    error_code: str | None
    available_at: Any
    created_at: Any
    completed_at: Any


class GatewayCommandStore:
    """Calls only the narrowly scoped SQL transition functions."""

    def __init__(self, pool):
        self.pool = pool

    async def _call(self, tenant_id: UUID, query: str, *args):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                return await conn.fetchrow(query, *args)

    async def submit_text(
        self,
        tenant_id: UUID,
        account_id: UUID,
        route_generation: int,
        operation_key: UUID,
        destination_peer_id: int,
        text: str,
    ) -> UUID:
        row = await self._call(
            tenant_id,
            """SELECT public.submit_gateway_account_command($1,$2,$3,$4,$5) AS id""",
            account_id,
            route_generation,
            operation_key,
            destination_peer_id,
            text,
        )
        if row is None or row["id"] is None:
            raise RuntimeError("Gateway command submission returned no identifier")
        return row["id"]

    async def get_result(
        self, tenant_id: UUID, command_id: UUID
    ) -> GatewayCommandResult | None:
        row = await self._call(
            tenant_id,
            """SELECT * FROM public.get_gateway_account_command_result($1)""",
            command_id,
        )
        if row is None:
            return None
        result = row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        return GatewayCommandResult(
            id=row["command_id"],
            account_id=row["account_id"],
            status=row["status"],
            result=dict(result) if result is not None else None,
            error_code=row["error_code"],
            available_at=row["available_at"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    async def claim_next(
        self,
        tenant_id: UUID,
        account_id: UUID,
        worker_id: str,
        fencing_token: int,
        claim_seconds: int = 15,
    ) -> GatewayCommand | None:
        row = await self._call(
            tenant_id,
            """SELECT * FROM public.claim_next_gateway_account_command($1,$2,$3,$4)""",
            account_id,
            worker_id,
            fencing_token,
            claim_seconds,
        )
        if row is None:
            return None
        return GatewayCommand(
            id=row["id"],
            tenant_id=row["tenant_id"],
            account_id=row["account_id"],
            route_generation=row["route_generation"],
            operation_key=row["operation_key"],
            operation_kind=row["operation_kind"],
            destination_peer_id=row["destination_peer_id"],
            payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"]),
            claim_worker_id=row["claim_worker_id"],
            claim_fencing_token=row["claim_fencing_token"],
            claim_until=row["claim_until"],
        )

    async def mark_dispatch_started(
        self, tenant_id: UUID, command_id: UUID, worker_id: str, fencing_token: int
    ) -> bool:
        row = await self._call(
            tenant_id,
            """SELECT public.mark_gateway_account_command_dispatch_started($1,$2,$3) AS ok""",
            command_id,
            worker_id,
            fencing_token,
        )
        return bool(row and row["ok"])

    async def finish(
        self,
        tenant_id: UUID,
        command_id: UUID,
        worker_id: str,
        fencing_token: int,
        status: str,
        *,
        remote_message_id: int | None = None,
        error_code: str | None = None,
        retry_after: int | None = None,
    ) -> bool:
        row = await self._call(
            tenant_id,
            """SELECT public.finish_gateway_account_command($1,$2,$3,$4,$5,$6,$7) AS ok""",
            command_id,
            worker_id,
            fencing_token,
            status,
            remote_message_id,
            error_code,
            retry_after,
        )
        return bool(row and row["ok"])

    async def requeue_flood_wait(
        self, tenant_id: UUID, account_id: UUID, worker_id: str, fencing_token: int
    ) -> int:
        row = await self._call(
            tenant_id,
            """SELECT public.requeue_ready_gateway_account_flood_wait($1,$2,$3) AS requeued""",
            account_id,
            worker_id,
            fencing_token,
        )
        return int(row["requeued"]) if row else 0

    async def recover_expired(
        self, tenant_id: UUID, account_id: UUID, worker_id: str, fencing_token: int
    ) -> tuple[int, int]:
        """Requeue only commands proven not to have started dispatch.

        Any expired claim with dispatch_started_at is made UNKNOWN and must be
        reconciled; this method never retries a potentially delivered message.
        """
        row = await self._call(
            tenant_id,
            """SELECT requeued_before_dispatch, marked_unknown
               FROM public.recover_expired_gateway_account_commands($1,$2,$3)""",
            account_id,
            worker_id,
            fencing_token,
        )
        if row is None:
            return (0, 0)
        return (int(row["requeued_before_dispatch"]), int(row["marked_unknown"]))
