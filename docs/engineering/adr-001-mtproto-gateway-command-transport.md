# ADR-001: Durable command transport for the MTProto Gateway

Status: Proposed for implementation under ASD-30
Scope: account broadcast only; no Bot API traffic

## Context

The current Gateway process owns one Telethon client per account lifecycle and holds the persisted account lease/fencing token. The lifecycle is intentionally read-only: connect, verify authorization and identity, then wait for stop, lease loss, or disconnect. The business broadcast worker runs in a separate process. There is currently no cross-process operation channel.

The existing account adapter must not remain an alternate owner: it currently creates its own TelethonClient from legacy StringSession configuration. Adding a second client inside a new worker would preserve the unsafe split ownership.

## Decision

Use a PostgreSQL-backed durable command inbox/outbox as the first transport. Do not expose a public HTTP endpoint, Telegram client, session material, or generic method invocation to business workers.

The Gateway process remains the sole MTProto connection owner. Business workers can submit only a narrowly typed account-broadcast command and poll its durable result.

## Proposed data model

### gateway_account_commands
- id UUID PRIMARY KEY
- tenant_id UUID NOT NULL
- account_id UUID NOT NULL
- route_generation BIGINT NOT NULL
- operation_key UUID NOT NULL
- operation_kind TEXT NOT NULL; first increment allows only preflight_send_text
- destination_peer_id BIGINT NOT NULL
- payload JSONB NOT NULL; strict schema, text only in the first increment
- status: queued, claimed, succeeded, rejected, flood_wait, unknown, cancelled
- claim_worker_id, claim_fencing_token, claim_until
- result JSONB NULL; allowlisted fields only
- error_code TEXT NULL; stable sanitized enum
- available_at, created_at, updated_at timestamps

Constraints: unique (tenant_id, account_id, operation_key); foreign key to existing account; tenant FORCE RLS; indexes for ready commands and expired claims.

### gateway_account_command_events
Append-only sanitized transitions. Never store message body, session, token, phone number, proxy credentials, raw Telegram objects or raw exception text.

## Submission and authorization

1. Account-broadcast worker claims a business delivery using the existing business outbox/task lease.
2. It creates or retrieves one Gateway command using a stable operation key derived from delivery identity and attempt generation. Repeated submissions return the same command.
3. Worker may submit only for the tenant/account bound to its validated business context. It cannot set a Gateway fencing token or claim a command.
4. Worker polls command status with bounded backoff. It cannot access Telegram session rows, decrypt material, or connect to Telegram.
5. Cancellation before Gateway claim may cancel a queued command. Cancellation after send dispatch begins cannot claim the send was prevented; result must be reconciled.

## Gateway claim and execution

1. GatewayRunner owns the account lease and already-authorized Telethon client.
2. Only the process holding the current lease/fencing token may claim commands for that account. Claim transaction checks active tenant/account, enabled route and matching generation, current unexpired lease, and queued/available command.
3. Gateway validates operation kind and payload again. No caller-supplied method name, request class, session string, proxy profile or arbitrary RPC arguments.
4. Immediately before Telegram send, recheck route generation and lease ownership. Do not hold a database transaction open across the network RPC.
5. Execute on the existing GatewayRunner client. No other TelethonClient is constructed.
6. Persist allowlisted result under the same command identity.

## Result state machine
- Confirmed Telegram message id: succeeded; persist peer/message id and completion time.
- Definitive permission/authorization/destination rejection: rejected with sanitized code.
- FloodWait: persist server-provided deadline; stop dispatch for that account until eligible. Do not rotate account or proxy.
- Timeout, crash, connection loss or lease loss after dispatch may have started: unknown. Never automatically resend.
- Route generation changed before dispatch: cancel/reject without network activity.
- Expired claimed command with dispatch_started_at: unknown, not automatically requeued.
- Only operator-approved reconciliation with evidence may resolve unknown.

## RLS and database role boundaries

Use separate least-privilege roles. The business worker may insert permitted command requests and read only its authorized tenant command status/result; it cannot update claim/fencing/status fields and cannot read session/material tables. Gateway worker may claim/update commands only within its explicit tenant allowlist. Runtime API has no claim privilege.

Prefer SECURITY DEFINER functions with fixed search_path for atomic insert/claim/transition if column grants cannot express the boundary. Revoke PUBLIC execute and grant only to exact worker roles. Every function validates tenant context and mapping rather than trusting supplied tenant ids.

## Backpressure and lifecycle
- One in-flight command per account initially; bounded queue depth and payload size.
- Fair round-robin scheduling across explicitly configured tenants/accounts.
- A paused/disabled route does not consume queued commands.
- SIGTERM stops new claims, allows bounded in-flight completion, then disconnects; uncertain disconnect quarantines the account.
- Metrics expose queue age, FloodWait, unknown result and command failure without message content.
- Bot API worker remains independent.

## Required tests before merge
1. Tenant isolation and RLS with real restricted PostgreSQL roles.
2. Only current lease/fencing token can claim or complete.
3. Old Gateway generation cannot execute after route disable/re-enable.
4. Duplicate operation key creates exactly one durable command.
5. Crash before dispatch is safely reclaimable.
6. Crash after dispatch becomes unknown and is not resent.
7. FloodWait persists across restart and blocks that account only.
8. Cancellation before claim prevents dispatch; after dispatch becomes unknown unless confirmed.
9. SIGTERM drain and disconnect-uncertain quarantine.
10. No worker constructs TelethonClient or reads StringSession/session material.

## Rollout sequence
1. Add schema and restricted SQL functions; test fresh, replay and upgrade migrations.
2. Add Gateway command repository and command loop behind a disabled feature flag.
3. Add fake-client Gateway dispatch tests.
4. Replace AccountBroadcast adapter with command submission/result polling; remove legacy credential reads and client construction.
5. Add independently pausable account-broadcast worker service.
6. Run unit and real PostgreSQL integration tests.
7. Enable only for a dedicated authorized Telegram test account and test group.
8. Create a Draft PR; do not merge PR #27 or this branch without independent review.

## Explicitly not solved
Telegram exactly-once delivery cannot be guaranteed across a crash after remote acceptance but before local confirmation. Safe state is unknown, followed by reconciliation. Channel cloning, media reuse, live rollout, KMS migration and real Telegram validation are out of scope.