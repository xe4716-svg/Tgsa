"""Serial command execution on the Runner-owned client.

Transport must be explicitly injected. No production Telegram sender is installed.
Any non-FloodWait exception after dispatch is conservatively unknown; FloodWait is
a known non-delivery result and may be retried only after its persisted delay.
"""
import asyncio

from telethon.errors import FloodWaitError


class CommandResultUncertain(RuntimeError):
    pass


class GatewayCommandExecutor:
    def __init__(self, store, sender):
        self.store, self.sender = store, sender

    async def execute_one(self, lease, generation, client, controlled, timeout):
        scope = (lease.tenant_id, lease.account_id, lease.worker_id, lease.fencing_token)
        _, unknown = await controlled(lambda: self.store.recover_expired(*scope), timeout)
        if unknown:
            raise CommandResultUncertain('Recovered uncertain dispatch')
        await controlled(lambda: self.store.requeue_flood_wait(*scope), timeout)
        command = await controlled(lambda: self.store.claim_next(*scope), timeout)
        if command is None:
            return False
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
                await asyncio.wait_for(self.store.finish(*args, 'flood_wait', retry_after=max(1, exc.seconds)), timeout)
            except Exception:
                pass
            # Always let the Runner persist the account-wide lease deferral,
            # including when the command-result database write failed.
            raise
        except BaseException:
            # This persistence attempt deliberately survives a signalled stop.
            # On DB failure/lease loss, expired-claim recovery records UNKNOWN.
            try:
                await asyncio.wait_for(self.store.finish(*args, 'unknown', error_code='DISPATCH_UNCERTAIN'), timeout)
            except Exception:
                pass
            raise CommandResultUncertain('Dispatch result uncertain') from None
        if not await asyncio.wait_for(self.store.finish(*args, 'succeeded', remote_message_id=message_id), timeout):
            raise CommandResultUncertain('Result persistence lost')
        return True
