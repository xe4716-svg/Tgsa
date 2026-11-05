import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from telethon.errors import FloodWaitError

from app.telegram.gateway_command_executor import (
    GatewayCommandExecutor,
    GatewayPreflightRejected,
    CommandResultUncertain,
)
from app.telegram.gateway_leases import AccountLease
from test_gateway_runtime import setup


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fixture():
    lease = AccountLease(uuid4(), uuid4(), "test", 7)
    command = SimpleNamespace(
        id=uuid4(),
        tenant_id=lease.tenant_id,
        account_id=lease.account_id,
        route_generation=3,
        claim_worker_id="test",
        claim_fencing_token=7,
        operation_kind="preflight_send_text",
        payload={"text": "synthetic"},
        destination_peer_id=123,
    )
    store = SimpleNamespace(
        recover_expired=AsyncMock(return_value=(0, 0)),
        requeue_flood_wait=AsyncMock(return_value=0),
        claim_next=AsyncMock(return_value=command),
        mark_dispatch_started=AsyncMock(return_value=True),
        finish=AsyncMock(return_value=True),
    )
    prepared = object()
    preflight = AsyncMock(return_value=prepared)
    sender = AsyncMock(return_value=42)
    return lease, command, store, preflight, sender, prepared


async def controlled(operation, timeout):
    return await asyncio.wait_for(operation(), timeout)


@pytest.mark.anyio
async def test_preflight_must_complete_before_dispatch_marker_and_sender():
    lease, command, store, preflight, sender, prepared = fixture()
    order = []

    async def do_preflight(client, actual_command):
        order.append("preflight")
        assert actual_command is command
        return prepared

    async def mark(*args):
        order.append("dispatch_started")
        return True

    async def do_send(client, actual_command, actual_prepared):
        order.append("send")
        assert actual_command is command
        assert actual_prepared is prepared
        return 42

    preflight.side_effect = do_preflight
    store.mark_dispatch_started.side_effect = mark
    sender.side_effect = do_send

    assert await GatewayCommandExecutor(store, preflight, sender).execute_one(
        lease, 3, object(), controlled, 1
    )
    assert order == ["preflight", "dispatch_started", "send"]
    store.finish.assert_awaited_once_with(
        lease.tenant_id, command.id, "test", 7, "succeeded", remote_message_id=42
    )


@pytest.mark.anyio
async def test_deterministic_preflight_rejection_is_recorded_without_dispatch():
    lease, command, store, preflight, sender, _ = fixture()
    preflight.side_effect = GatewayPreflightRejected("DESTINATION_FORBIDDEN")

    assert await GatewayCommandExecutor(store, preflight, sender).execute_one(
        lease, 3, object(), controlled, 1
    )

    store.finish.assert_awaited_once_with(
        lease.tenant_id,
        command.id,
        "test",
        7,
        "rejected",
        error_code="DESTINATION_FORBIDDEN",
    )
    store.mark_dispatch_started.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.anyio
async def test_preflight_flood_wait_is_persisted_without_dispatch_marker():
    lease, command, store, preflight, sender, _ = fixture()
    preflight.side_effect = FloodWaitError(request=None, capture=120)

    with pytest.raises(FloodWaitError):
        await GatewayCommandExecutor(store, preflight, sender).execute_one(
            lease, 3, object(), controlled, 1
        )

    store.finish.assert_awaited_once_with(
        lease.tenant_id, command.id, "test", 7, "flood_wait", retry_after=120
    )
    store.mark_dispatch_started.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.anyio
async def test_preflight_transport_error_never_crosses_dispatch_boundary():
    lease, command, store, preflight, sender, _ = fixture()
    preflight.side_effect = OSError("private preflight diagnostic")

    with pytest.raises(OSError):
        await GatewayCommandExecutor(store, preflight, sender).execute_one(
            lease, 3, object(), controlled, 1
        )

    store.finish.assert_not_awaited()
    store.mark_dispatch_started.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), OSError("private diagnostic"), asyncio.CancelledError()],
)
async def test_uncertain_result_after_dispatch_never_retries_or_leaks_diagnostics(failure):
    lease, command, store, preflight, sender, _ = fixture()
    sender.side_effect = failure

    with pytest.raises(CommandResultUncertain, match="Dispatch result uncertain"):
        await GatewayCommandExecutor(store, preflight, sender).execute_one(
            lease, 3, object(), controlled, 1
        )

    assert sender.await_count == 1
    assert store.finish.await_args.args[-1] == "unknown"
    assert store.finish.await_args.kwargs == {"error_code": "DISPATCH_UNCERTAIN"}


@pytest.mark.anyio
@pytest.mark.parametrize("persistence", ["ok", "refused", "failed"])
async def test_send_flood_wait_is_persisted_before_runner_defers_lease(persistence):
    lease, command, store, preflight, sender, _ = fixture()
    sender.side_effect = FloodWaitError(request=None, capture=120)
    if persistence == "refused":
        store.finish.return_value = False
    if persistence == "failed":
        store.finish.side_effect = OSError("database unavailable")

    with pytest.raises(FloodWaitError):
        await GatewayCommandExecutor(store, preflight, sender).execute_one(
            lease, 3, object(), controlled, 1
        )

    assert store.finish.await_args.args[-1] == "flood_wait"
    assert store.finish.await_args.kwargs == {"retry_after": 120}
    assert sender.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["scope", "mark", "recovery"])
async def test_failed_boundary_never_calls_transport(boundary):
    lease, command, store, preflight, sender, _ = fixture()
    if boundary == "scope":
        command.route_generation = 4
    if boundary == "mark":
        store.mark_dispatch_started.return_value = False
    if boundary == "recovery":
        store.recover_expired.return_value = (0, 1)

    with pytest.raises(CommandResultUncertain):
        await GatewayCommandExecutor(store, preflight, sender).execute_one(
            lease, 3, object(), controlled, 1
        )

    preflight.assert_not_awaited() if boundary == "recovery" else None
    sender.assert_not_awaited()
    if boundary == "recovery":
        store.requeue_flood_wait.assert_not_awaited()
    if boundary == "mark":
        preflight.assert_awaited_once()


@pytest.mark.anyio
async def test_unacknowledged_result_stops_execution():
    lease, command, store, preflight, sender, _ = fixture()
    store.finish.return_value = False

    with pytest.raises(CommandResultUncertain, match="Result persistence lost"):
        await GatewayCommandExecutor(store, preflight, sender).execute_one(
            lease, 3, object(), controlled, 1
        )

    sender.assert_awaited_once()


@pytest.mark.anyio
async def test_runner_dispatches_with_explicit_preflight_then_fake_sender_and_drains():
    lease, command, store, preflight, sender, prepared = fixture()
    stop = asyncio.Event()
    runner, leases, client, _ = await setup(
        command_store=store,
        command_preflight=preflight,
        command_sender=sender,
    )
    command.tenant_id = leases.lease.tenant_id
    command.account_id = leases.lease.account_id
    command.claim_fencing_token = leases.lease.fencing_token
    leases.conn.fetchval = AsyncMock(return_value=True)

    async def finish(*args, **kwargs):
        stop.set()
        return True

    store.finish.side_effect = finish
    assert (
        await runner.run(
            command.tenant_id,
            command.account_id,
            stop,
            route_generation=3,
        )
    ).reason == "STOPPED"
    preflight.assert_awaited_once_with(client, command)
    sender.assert_awaited_once_with(client, command, prepared)
    assert leases.calls.index("disconnect") < leases.calls.index("release")


@pytest.mark.anyio
async def test_runner_unknown_stops_and_disconnects():
    lease, command, store, preflight, sender, _ = fixture()
    sender.side_effect = TimeoutError()
    runner, leases, client, _ = await setup(
        command_store=store,
        command_preflight=preflight,
        command_sender=sender,
    )
    command.tenant_id = leases.lease.tenant_id
    command.account_id = leases.lease.account_id
    command.claim_fencing_token = 1
    leases.conn.fetchval = AsyncMock(return_value=True)

    result = await runner.run(
        command.tenant_id,
        command.account_id,
        asyncio.Event(),
        route_generation=3,
    )
    assert result.reason == "COMMAND_UNCERTAIN"
    preflight.assert_awaited_once_with(client, command)
    sender.assert_awaited_once()
    client.disconnect.assert_awaited_once()
