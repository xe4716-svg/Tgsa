from __future__ import annotations

import asyncio
import json
import logging
import re
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


async def _update_sweep_job(
    pool,
    tenant_id,
    job_id,
    *,
    status: str,
    error_code: str | None = None,
    delay_seconds: int = 0,
    txid: str | None = None,
    fee_sun: int | None = None,
    signer_reference: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            await conn.execute(
                """
                UPDATE tron_sweep_jobs
                SET status = $3,
                    last_error_code = $4,
                    next_attempt_at = NOW() + ($5 * INTERVAL '1 second'),
                    txid = COALESCE($6, txid),
                    fee_sun = COALESCE($7, fee_sun),
                    signer_reference = COALESCE($8, signer_reference),
                    submitted_at = CASE WHEN $3 = 'SUBMITTED' THEN NOW() ELSE submitted_at END,
                    confirmed_at = CASE WHEN $3 = 'CONFIRMED' THEN NOW() ELSE confirmed_at END,
                    updated_at = NOW()
                WHERE tenant_id = $1 AND id = $2
                """,
                tenant_id,
                job_id,
                status,
                error_code,
                delay_seconds,
                txid,
                fee_sun,
                signer_reference,
            )


async def _submit_sweep_job(pool, client: httpx.AsyncClient, job) -> None:
    if not (
        settings.tron_sweep_enabled
        and settings.tron_sweep_signer_url
        and settings.tron_sweep_signer_token
        and settings.tron_treasury_address
        and settings.tron_sweep_max_fee_sun > 0
    ):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="RETRY",
            error_code="SWEEP_SIGNER_NOT_CONFIGURED",
            delay_seconds=3600,
        )
        return

    payload = {
        "job_id": str(job["id"]),
        "order_id": str(job["topup_order_id"]),
        "derivation_index": int(job["derivation_index"]),
        "from_address": job["source_address"],
        "to_address": job["destination_address"],
        "token_contract": job["token_contract"],
        "amount_minor": int(job["amount_minor"]),
        "max_fee_sun": int(job["max_fee_sun"]),
    }
    signer_headers = {"Authorization": f"Bearer {settings.tron_sweep_signer_token}"}
    quote_url = settings.tron_sweep_signer_url.rstrip("/") + "/v1/tron/usdt/quote"
    try:
        quote_response = await client.post(
            quote_url,
            json={
                "order_id": str(job["topup_order_id"]),
                "derivation_index": int(job["derivation_index"]),
                "from_address": job["source_address"],
                "to_address": job["destination_address"],
                "token_contract": job["token_contract"],
            },
            headers=signer_headers,
        )
        quote_response.raise_for_status()
        quote = quote_response.json()
        available_amount = int(quote["available_amount_minor"])
        estimated_fee = int(quote["estimated_fee_sun"])
        can_sweep = quote.get("can_sweep") is True
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        status = "REVIEW" if int(job["attempt_count"]) >= 3 else "RETRY"
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status=status,
            error_code="SIGNER_QUOTE_FAILED",
            delay_seconds=3600,
        )
        return

    if available_amount < int(job["amount_minor"]):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SWEEP_BALANCE_BELOW_EXPECTED",
        )
        return
    if estimated_fee < 0 or estimated_fee > int(job["max_fee_sun"]):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="FEE_LIMIT_EXCEEDED",
            fee_sun=estimated_fee,
        )
        return
    if not can_sweep:
        status = "REVIEW" if int(job["attempt_count"]) >= 3 else "RETRY"
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status=status,
            error_code="INSUFFICIENT_TRX_OR_RESOURCES",
            delay_seconds=3600,
            fee_sun=estimated_fee,
        )
        return

    url = settings.tron_sweep_signer_url.rstrip("/") + "/v1/tron/usdt/sweep"
    try:
        response = await client.post(
            url,
            json=payload,
            headers={
                **signer_headers,
                "Idempotency-Key": str(job["id"]),
            },
        )
    except httpx.HTTPError:
        status = "REVIEW" if int(job["attempt_count"]) >= 3 else "RETRY"
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status=status,
            error_code="SIGNER_UNREACHABLE",
            delay_seconds=3600,
        )
        return

    if response.status_code >= 300:
        try:
            error = response.json()
        except ValueError:
            error = {}
        error_code = str(error.get("code") or f"SIGNER_HTTP_{response.status_code}")[:80]
        if error_code == "FEE_LIMIT_EXCEEDED":
            status = "REVIEW"
        else:
            status = "REVIEW" if int(job["attempt_count"]) >= 3 else "RETRY"
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status=status,
            error_code=error_code,
            delay_seconds=3600,
        )
        return

    try:
        result = response.json()
        txid = str(result["txid"])
        fee_sun = int(result["fee_sun"])
        result_amount = int(result["amount_minor"])
        signer_status = str(result.get("status", "")).upper()
    except (ValueError, TypeError, KeyError):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SIGNER_RESPONSE_INVALID",
        )
        return

    if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SIGNER_TXID_INVALID",
        )
        return
    if result_amount != int(job["amount_minor"]):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SIGNER_AMOUNT_MISMATCH",
            txid=txid,
            fee_sun=fee_sun,
        )
        return
    if fee_sun < 0 or fee_sun > int(job["max_fee_sun"]):
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SIGNER_FEE_LIMIT_BREACHED",
            txid=txid,
            fee_sun=fee_sun,
        )
        return
    if signer_status not in {"SUBMITTED", "ALREADY_SUBMITTED", "CONFIRMED"}:
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SIGNER_STATUS_INVALID",
            txid=txid,
            fee_sun=fee_sun,
        )
        return

    await _update_sweep_job(
        pool,
        job["tenant_id"],
        job["id"],
        status="SUBMITTED",
        txid=txid.lower(),
        fee_sun=fee_sun,
        signer_reference=str(result.get("reference") or "")[:160] or None,
    )


async def _confirm_sweep_job(
    pool,
    client: httpx.AsyncClient,
    job,
) -> None:
    url = settings.tron_api_base_url.rstrip("/") + "/walletsolidity/gettransactioninfobyid"
    try:
        response = await client.post(
            url,
            json={"value": job["txid"]},
            headers={"TRON-PRO-API-KEY": settings.trongrid_api_key or ""},
        )
        response.raise_for_status()
        receipt_data = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("TRC20 sweep confirmation query failed job=%s", job["id"])
        return

    if receipt_data.get("Error") or not receipt_data.get("id"):
        # Absence from the solidified node is not proof of failure. Query again later.
        return
    if str(receipt_data.get("id")).lower() != str(job["txid"]).lower():
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="REVIEW",
            error_code="SOLIDITY_TXID_MISMATCH",
        )
        return
    receipt = receipt_data.get("receipt") or {}
    execution = str(receipt.get("result") or "").upper()
    top_level = str(receipt_data.get("result") or "").upper()
    if execution == "SUCCESS" and top_level != "FAILED":
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="CONFIRMED",
            fee_sun=int(receipt_data.get("fee") or job.get("fee_sun") or 0),
        )
    elif execution == "FAILED" or top_level == "FAILED":
        await _update_sweep_job(
            pool,
            job["tenant_id"],
            job["id"],
            status="FAILED",
            error_code="ONCHAIN_EXECUTION_FAILED",
        )


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

                            sweep_ready = bool(
                                settings.tron_sweep_enabled
                                and settings.tron_sweep_signer_url
                                and settings.tron_sweep_signer_token
                                and settings.tron_treasury_address
                                and settings.tron_sweep_max_fee_sun > 0
                            )
                            if sweep_ready:
                                pending_sweeps = await lock_conn.fetch(
                                    "SELECT * FROM claim_pending_tron_sweeps($1)",
                                    batch_size,
                                )
                                for job in pending_sweeps:
                                    if stop_event.is_set():
                                        break
                                    await _submit_sweep_job(pool, client, job)

                                submitted_sweeps = await lock_conn.fetch(
                                    "SELECT * FROM claim_submitted_tron_sweeps($1)",
                                    batch_size,
                                )
                                for job in submitted_sweeps:
                                    if stop_event.is_set():
                                        break
                                    await _confirm_sweep_job(pool, client, job)
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
