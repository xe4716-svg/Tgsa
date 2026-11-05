"""Serial command execution on the Runner-owned client.

Preflight is read-only and must not send messages. The dispatch marker is written
only after preflight succeeds. Any non-FloodWait exception after dispatch is
conservatively unknown; FloodWait is a known non-delivery result.
"""
import asyncio
import re

from telethon.errors import FloodWaitError


class CommandResultUncertain(RuntimeError):
    pass


class GatewayPreflightRejected(RuntimeError):
    """A deterministic, known non-delivery result from read-only preflight."""

    def __init__(self, error_code: str):
        if not isinstance(error_code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code) is None:
            raise ValueError("Invalid Gateway preflight error code")
        self.error_code = error_code
        super().__init__("Gateway preflight rejected")


class GatewayCommandExecutor:
    def __init__(self, store, preflight, sender):
        if not callable(preflight) or not callable(sender):
            raise ValueError("Explicit preflight and sender callables are required")
        self.store, self.preflight, self.sender = store, preflight, sender

    async def _persist_flood_wait(self, args, exc, timeout):
        # This persistence attempt deliberately survives a signalled stop.
        # If it fails, the undispatched/claimed command can be safely recovered
        # because dispatch_started_at has not been set (preflight) or its state
        # remains conservatively protected by the existing claim recovery.
        try:
            await asyncio.wait_for(
                self.store.finish(*args, "flood_wait", retry_after=max(1, exc.seconds)),
                timeout,
            )
        except Exception:
            pass

    async def execute_one(self, lease, generation, client, controlled, timeout):
        scope = (lease.tenant_id, lease.account_id, lease.worker_id, lease.fencing_token)
        _, unknown = await controlled(lambda: self.store.recover_expired(*scope), timeout)
        if unknown:
            raise CommandResultUncertain("Recovered uncertain dispatch")
        await controlled(lambda: self.store.requeue_flood_wait(*scope), timeout)
        command = await controlled(lambda: self.store.claim_next(*scope), timeout)
        if command is None:
            return False

        args = (lease.tenant_id, command.id, lease.worker_id, lease.fencing_token)
        if (
            command.tenant_id != lease.tenant_id
            or command.account_id != lease.account_id
            or command.route_generation != generation
            or command.claim_worker_id != lease.worker_id
            or command.claim_fencing_token != lease.fencing_token
            or command.operation_kind != "preflight_send_text"
        ):
            raise CommandResultUncertain("Command scope mismatch")

        # Preflight may resolve/validate a destination and check current write
        # capability, but must not issue the send RPC or mutate remote state.
        try:
            prepared = await controlled(lambda: self.preflight(client, command), timeout)
        except GatewayPreflightRejected as exc:
            try:
                persisted = await asyncio.wait_for(
                    self.store.finish(*args, "rejected", error_code=exc.error_code),
                    timeout,
                )
            except Exception:
                persisted = False
            if not persisted:
                raise CommandResultUncertain("Preflight rejection persistence lost") from None
            return True
        except FloodWaitError as exc:
            await self._persist_flood_wait(args, exc, timeout)
            # Runner persists the account-wide lease deferral before resuming.
            raise

        # No Telegram send RPC is allowed until the dispatch boundary is durably
        # acknowledged under the same tenant/account/worker/fencing scope.
        if not await controlled(lambda: self.store.mark_dispatch_started(*args), timeout):
            raise CommandResultUncertain("Dispatch authorization lost")

        try:
            message_id = await controlled(
                lambda: self.sender(client, command, prepared),
                timeout,
            )
            if type(message_id) is not int or message_id <= 0:
                raise ValueError("Invalid remote result")
        except FloodWaitError as exc:
            await self._persist_flood_wait(args, exc, timeout)
            # Always let the Runner persist the account-wide lease deferral,
            # including when the command-result database write failed.
            raise
        except BaseException:
            # Once dispatch_started is durable, every non-FloodWait failure is
            # UNKNOWN: a network request may have reached Telegram.
            try:
                await asyncio.wait_for(
                    self.store.finish(*args, "unknown", error_code="DISPATCH_UNCERTAIN"),
                    timeout,
                )
            except Exception:
                pass
            raise CommandResultUncertain("Dispatch result uncertain") from None

        if not await asyncio.wait_for(
            self.store.finish(*args, "succeeded", remote_message_id=message_id),
            timeout,
        ):
            raise CommandResultUncertain("Result persistence lost")
        return True
