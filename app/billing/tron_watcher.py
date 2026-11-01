from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from bip_utils import Bip44, Bip44Changes, Bip44Coins, Bip44Levels

from app.billing.payments import _process_verified_payment
from app.config import settings


logger = logging.getLogger(__name__)
_TRON_WATCHER_LOCK_ID = 73190527


def derive_tron_deposit_address(account_xpub: str | None, index: int) -> str:
    """Derive a TRON address from a public BIP44 account or external-chain key."""
    if not account_xpub or index < 0:
        raise ValueError("A valid TRON account xpub and non-negative index are required")
    if account_xpub.startswith(("xprv", "tprv")):
        raise ValueError("TRON deposit derivation accepts public extended keys only")
    try:
        context = Bip44.FromExtendedKey(account_xpub, Bip44Coins.TRON)
        if not context.IsPublicOnly():
            raise ValueError("TRON deposit derivation accepts public extended keys only")
        if context.Level() == Bip44Levels.ACCOUNT:
            context = context.Change(Bip44Changes.CHAIN_EXT)
        elif context.Level() != Bip44Levels.CHANGE:
            raise ValueError(
                "TRON_ACCOUNT_XPUB must be at m/44'/195'/0' or m/44'/195'/0'/0"
            )
        address = context.AddressIndex(index).PublicKey().ToAddress()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("TRON_ACCOUNT_XPUB is not a valid TRON BIP44 public key") from exc
    if not isinstance(address, str) or not address.startswith("T"):
        raise ValueError("Derived TRON address is invalid")
    return address


async def _fetch_confirmed_usdt_transfers(
    client: httpx.AsyncClient,
    address: str,
) -> list[dict[str, Any]]:
    url = (
        settings.tron_api_base_url.rstrip("/")
        + f"/v1/accounts/{address}/transactions/trc20"
    )
    response = await client.get(
        url,
        params={
            "only_confirmed": "true",
            "limit": "200",
            "contract_address": settings.tron_usdt_contract,
        },
        headers={"TRON-PRO-API-KEY": settings.trongrid_api_key or ""},
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", [])
    return rows if isinstance(rows, list) else []


async def _scan_deposit(
    pool,
    client: httpx.AsyncClient,
    deposit,
) -> int:
    credited = 0
    address = deposit["deposit_address"]
    try:
        transfers = await _fetch_confirmed_usdt_transfers(client, address)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("TRC20 scan failed for deposit %s: %s", deposit["order_no"], type(exc).__name__)
        return 0

    for transfer in transfers:
        token = transfer.get("token_info") or {}
        if token.get("address") != settings.tron_usdt_contract:
            continue
        if int(token.get("decimals", 6)) != 6:
            continue
        if transfer.get("to") != address:
            continue
        transaction_id = transfer.get("transaction_id")
        raw_value = transfer.get("value")
        if not transaction_id or raw_value is None:
            continue
        try:
            amount_minor = int(raw_value)
        except (TypeError, ValueError):
            continue
        if amount_minor <= 0:
            continue

        details = {
            "event_id": str(transaction_id),
            "order_no": deposit["order_no"],
            "provider_transaction_id": str(transaction_id),
            "amount_minor": amount_minor,
            "currency": "USDT",
        }
        raw = json.dumps(transfer, sort_keys=True, separators=(",", ":")).encode("utf-8")
        result = await _process_verified_payment(pool, "USDT", details, raw)
        if result == "credited":
            credited += 1
            logger.info(
                "Credited confirmed TRC20 deposit order=%s tx=%s",
                deposit["order_no"],
                transaction_id,
            )
        elif result == "payment_mismatch":
            logger.error(
                "TRC20 deposit requires manual review order=%s tx=%s",
                deposit["order_no"],
                transaction_id,
            )
    return credited


async def _wait_or_stop(stop_event: asyncio.Event, seconds: int) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_tron_watcher(pool, stop_event: asyncio.Event) -> None:
    """Run one database-leased confirmed-transfer watcher across API replicas."""
    if not settings.tron_watcher_enabled:
        return
    if not settings.tron_account_xpub or not settings.trongrid_api_key:
        logger.error("TRC20 watcher is enabled but the xpub or TronGrid API key is missing")
        return

    interval = min(max(settings.tron_watch_interval_seconds, 30), 600)
    batch_size = min(max(settings.tron_watch_batch_size, 1), 100)

    while not stop_event.is_set():
        async with pool.acquire() as lock_conn:
            acquired = await lock_conn.fetchval(
                "SELECT pg_try_advisory_lock($1)",
                _TRON_WATCHER_LOCK_ID,
            )
            if not acquired:
                await _wait_or_stop(stop_event, interval)
                continue

            logger.info("TRC20 confirmed-transfer watcher acquired its database lease")
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    while not stop_event.is_set():
                        try:
                            deposits = await lock_conn.fetch(
                                "SELECT * FROM claim_pending_tron_deposits($1)",
                                batch_size,
                            )
                            for deposit in deposits:
                                if stop_event.is_set():
                                    break
                                await _scan_deposit(pool, client, deposit)
                        except Exception:
                            logger.exception("TRC20 watcher iteration failed")
                        await _wait_or_stop(stop_event, interval)
            finally:
                try:
                    await lock_conn.fetchval(
                        "SELECT pg_advisory_unlock($1)",
                        _TRON_WATCHER_LOCK_ID,
                    )
                except Exception:
                    logger.warning("TRC20 watcher database lease could not be released cleanly")
            return
