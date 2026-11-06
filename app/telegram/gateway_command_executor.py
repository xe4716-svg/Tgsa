"""Serial command execution on the Runner-owned client.

Transport must be explicitly injected. No production Telegram sender is installed.
Any exception after dispatch is conservatively unknown; never retry that RPC.
"""
import asyncio

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from telethon.errors import FloodWaitError

from app.telemetry import extract_trace_context


class CommandResultUncertain(RuntimeError):
    pass


class GatewayCommandExecutor:
    def __init__(self, store, sender, *, tracer=None):
        self.store, self.sender = store, sender
        self.tracer = tracer or trace.get_tracer("tgmate.gateway.commands", "1.0.0")

    async def execute_one(self, lease, generation, client, controlled, timeout):
        scope = (lease.tenant_id, lease.account_id, lease.worker_id, lease.fencing_token)
        _, unknown = await controlled(lambda: self.store.recover_expired(*scope), timeout)
        if unknown:
            raise CommandResultUncertain('Recovered uncertain dispatch')
        command = await controlled(lambda: self.store.claim_next(*scope), timeout)
        if command is None:
            return False

        parent_context = extract_trace_context(getattr(command, "traceparent", None))
        with self.tracer.start_as_current_span(
            "gateway.command.execute",
            context=parent_context,
            kind=SpanKind.CONSUMER,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            # Only bounded, non-identifying attributes are allowed. Never attach
            # payload, peer, tenant/account IDs, operation keys or exception text.
            span.set_attribute("gateway.command.kind", command.operation_kind)
            span.set_attribute("gateway.route.generation", generation)
            try:
                result = await self._dispatch(command, lease, generation, client, controlled, timeout)
                span.set_attribute("gateway.command.outcome", "SUCCEEDED" if result else "IDLE")
                return result
            except FloodWaitError as exc:
                span.set_attribute("gateway.command.outcome", "FLOOD_WAIT")
                span.set_attribute("gateway.retry_after_seconds", max(1, int(exc.seconds)))
                raise
            except CommandResultUncertain:
                span.set_attribute("gateway.command.outcome", "UNKNOWN")
                raise
            except BaseException as exc:
                span.set_attribute("gateway.command.outcome", "ERROR")
                span.set_attribute("exception.type", type(exc).__name__)
                raise

    async def _dispatch(self, command, lease, generation, client, controlled, timeout):
        args = (lease.tenant_id, command.id, lease.worker_id, lease.fencing_token)
        if (command.tenant_id != lease.tenant_id or command.account_id != lease.account_id
                or command.route_generation != generation
                or command.claim_worker_id != lease.worker_id
                or command.claim_fencing_token != lease.fencing_token
                or command.operation_kind != 'preflight_send_text'):
            raise CommandResultUncertain('Command scope mismatch')
        # Even an interrupted mark can have committed. Recovery must classify it
        # conservatively; no RPC is issued unless the mark is acknowledged.
        if not await controlled(lambda: self.store.mark_dispatch_started(*args), timeout):
            raise CommandResultUncertain('Dispatch authorization lost')
        try:
            message_id = await controlled(lambda: self.sender(client, command), timeout)
            if type(message_id) is not int or message_id <= 0:
                raise ValueError('Invalid remote result')
        except FloodWaitError as exc:
            try:
                await asyncio.wait_for(
                    self.store.finish(*args, 'flood_wait', retry_after=max(1, exc.seconds)),
                    timeout,
                )
            except Exception:
                pass
            # Always let the Runner persist the account-wide lease deferral,
            # including when the command-result database write failed.
            raise
        except BaseException:
            # This persistence attempt deliberately survives a signalled stop.
            # On DB failure/lease loss, expired-claim recovery records UNKNOWN.
            try:
                await asyncio.wait_for(
                    self.store.finish(*args, 'unknown', error_code='DISPATCH_UNCERTAIN'),
                    timeout,
                )
            except Exception:
                pass
            raise CommandResultUncertain('Dispatch result uncertain') from None
        if not await asyncio.wait_for(
            self.store.finish(*args, 'succeeded', remote_message_id=message_id),
            timeout,
        ):
            raise CommandResultUncertain('Result persistence lost')
        return True
