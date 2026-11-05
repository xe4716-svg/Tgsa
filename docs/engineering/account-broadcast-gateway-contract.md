# Account Broadcast → MTProto Gateway Contract

Status: implementation contract for ASD-30; not an implementation or proof of Telegram integration.

## Verified baseline

- `app/telegram/gateway_runtime.py` owns a single read-only MTProto connection lifecycle. It validates the persisted route generation, acquires/renews a fenced account lease, checks authorization and Telegram identity, watches session/proxy material revisions, and quarantines an account when disconnect cannot be confirmed.
- `app/telegram/gateway_supervisor.py` starts only explicitly allowlisted tenant/account routes, enforces global and per-tenant capacity, and does not automatically reconnect failed accounts.
- `app/workers/bot.py` registers the `bot_broadcast` business queue, but not `account_broadcast`.
- `docker-compose.workers.yml` has no account-broadcast service.
- `workstreams/tgmate-v2/app/telegram_adapters.py` currently creates a Telethon client from the legacy account credential setting. This is a separate credential and connection path from the root Gateway.
- `workstreams/tgmate-v2/app/broadcast_worker.py` supports `account_broadcast` in its queue selection and passes an `operation_key`, but its adapter call is not yet backed by the root Gateway lifecycle.

## Required architecture

1. **Gateway-owned connection only.** Business workers must never construct `TelegramClient`, read StringSession values, decrypt Telegram session material, or choose/switch proxies.
2. **Narrow operation request.** The worker submits a typed request containing:
   - `tenant_id`
   - `account_id`
   - `route_generation`
   - `operation_key` (stable delivery id)
   - allowlisted operation kind and validated destination/content reference
3. **Gateway revalidation.** Before the RPC, the Gateway must verify:
   - the current route is enabled and generation matches;
   - the caller is an authorized internal worker;
   - the account is active and still authorized;
   - the current lease fencing token is held by this Gateway process;
   - the session/proxy material revision has not changed.
4. **Single owner.** All account RPCs for an account pass through the same Gateway-owned Telethon connection and lease. No parallel client per task.
5. **Send result state machine.**
   - confirmed Telegram response → persist remote message reference and complete delivery;
   - FloodWait → persist server-provided wait and defer that account route;
   - timeout/disconnect after send may have started → mark `unknown`, require reconciliation, never blindly resend;
   - confirmed pre-send rejection → fail or retry only under a bounded, explicit policy.
6. **Credential migration.** Replace legacy `settings.account_credentials` use with the existing encrypted `telegram_sessions` + SessionVault + Gateway material path. Do not copy plaintext session strings into worker environment or database fields.
7. **Dedicated worker.** Add a separate account-broadcast worker process/service with explicit tenant allowlist, bounded capacity, fair scheduling, task lease/fencing, SIGTERM drain, pause/resume, and redacted audit events. Keep Bot API broadcast isolated.
8. **No bypass on fallback.** If Gateway is unavailable, the job waits/pauses. It must not fall back to constructing its own Telethon client.

## Implementation sequence

1. Define a narrow internal Gateway RPC contract and authenticated transport.
2. Add Gateway-side operation dispatch on the already-managed client; preserve the existing read-only lifecycle API for other callers.
3. Add authorization and fencing checks at dispatch and immediately before send.
4. Adapt AccountBroadcast to call the Gateway contract; remove local Telethon client construction and legacy credential reads.
5. Add the dedicated worker service and lifecycle controls.
6. Add tests for concurrent lease ownership, route-generation changes, session revocation, FloodWait persistence, send-unknown reconciliation, process restart, SIGTERM drain, and tenant isolation.
7. Run unit + real PostgreSQL integration tests. Only then, with a dedicated authorized Telegram test account and test destination, perform external integration tests.

## Acceptance gates

- No business-worker Telethon client construction or plaintext session loading.
- Exactly one Gateway owner per Telegram account, enforced by persisted lease/fencing.
- All MTProto RPCs use the Gateway-managed client.
- Unknown send outcomes are reconciled rather than resent.
- FloodWait is persisted and respected.
- Worker can be paused/resumed independently without affecting Bot API workers or other modules.
- CI is green and a separate reviewed PR is available.
- Real Telegram integration is reported separately from mocked/sandbox tests.

## Explicit non-goals

- This task does not implement channel cloning.
- It does not merge PR #27 or modify `main`.
- It does not assume production Telegram credentials are available.
