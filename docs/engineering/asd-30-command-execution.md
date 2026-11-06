# ASD-30 controlled command execution

## Scope and current boundary

The command executor runs serially inside the existing GatewayRunner lifecycle,
using its owned client, lease, heartbeat, material checks and disconnect cleanup.
It requires both an explicitly injected command store and sender. The default
Gateway worker remains lifecycle-only: no real Telegram sender is installed and
no Telegram RPC was exercised during this work. Fake transports receive the
owned client and a scoped command, and return a positive remote message ID.

This is not completion of ASD-30: the AccountBroadcast adapter still needs its
legacy Telethon/session path removed, and the independent broadcast worker,
deployment wiring and operator reconciliation/resume API remain outstanding.
Do not deploy this as a complete account-broadcast service.

## Database authorization

Migration 017 creates three NOLOGIN, NOSUPERUSER, NOBYPASSRLS capability roles:

* `tgs_command_owner`: owns only the narrow transition/helper functions, with
  explicit table privileges. FORCE RLS remains enabled. Mapping lookup has a
  dedicated owner policy; command/account rows use the root tenant context.
* `tgs_command_submitter`: EXECUTE only on submit, no table/session privileges.
* `tgs_command_executor`: EXECUTE only on claim, mark, finish and recovery.

An administrator must create separate restricted LOGIN principals, grant the
appropriate capability group, and populate `gateway_command_principals` with
each login's root tenant and `submit`/`execute` capability. No credentials or
principals are automatically provisioned in deployment. Do not grant the owner
role, tgs_runtime, BYPASSRLS or administrative roles to these logins. The ACL uses
`session_user`, so changing a tenant GUC or SET ROLE cannot impersonate another
login. Removing an ACL row revokes that login's command capability; remove ACL
rows before recycling login names.

Submit takes a **business** tenant context; authorization resolves and locks its
existing mapping and validates both tenant states, then switches to the mapped
**root** context for account/route access. Executor operations take root context.
GatewayCommandStore encloses each call in a transaction, so context changes end
with that call. This command role is deliberately not a replacement for the
existing Gateway lifecycle/material database role. Production should use a
separate command pool; dedicated lease/material APIs are a remaining boundary.

Transitions lock tenant, account, route and lease in a consistent order, then
recheck fencing, generation and state. A second dispatch mark is refused. Only
one claimed command is permitted per account; unknown and flood_wait commands
block subsequent claims. FloodWait also flows to the Runner's existing durable
lease deferral even if writing the command result fails. Resuming flood_wait or
unknown commands requires a future explicit reconciliation workflow; no automatic
resubmission is provided.

Any interrupted or failed RPC after dispatch is UNKNOWN, never retried. A result
write failure stops the Runner; recovery marks an expired dispatched claim
unknown. Expired claims without a dispatch mark alone may requeue. The supervisor
pauses routes after COMMAND_UNCERTAIN using its existing unexpected-result path.
Database fencing cannot cancel a request already issued to Telegram.

## Migration compatibility

013 contained invalid `AS $ ... $;` PL/pgSQL quoting and could not execute as
committed. This change repairs that syntax. 014–016 are preserved; 017 replaces
their functions with the authorization/locking fixes. If a deployment has an
externally repaired and tracked 013, the migration hash check must reject drift;
review its applied SQL and perform a separate adoption procedure. Do not edit
the ledger or bypass hash checks automatically. Empty-database root + business
migrations were exercised with the normal tracked runners.

## Reproduce controlled PostgreSQL tests

Use a disposable PostgreSQL cluster, not production. Provide PGHOST, PGPORT,
PGUSER, PGPASSWORD (if required), PG_PSQL and TGMATE_COMMAND_INTEGRATION=1, then:

```
python -m pytest tests/test_gateway_commands_database.py tests/test_gateway_command_executor.py tests/test_gateway_runtime.py tests/test_gateway_supervisor.py tests/test_gateway_worker.py tests/test_gateway_router.py tests/test_migrations.py -q
```

The integration test creates and drops a child database, creates restricted test
logins, applies all root and business migrations, checks actual permissions/RLS,
concurrent claims, idempotency, generation/fencing, recovery and fake RPC results.
It restores the existing tgs_runtime password after migration and drops its test
logins. Run serially: migrations contain cluster-wide role provisioning.

## Local validation, 2026-11-03 (Asia/Shanghai)

* Full repository cloned at baseline `0211fb1` on the requested fix branch; the
  original uncommitted workspace was preserved.
* Local isolated PostgreSQL on 127.0.0.1:55439; synthetic data only. The local test
  cluster uses trust authentication on loopback and must be stopped after use.
* Root 001–017 and the complete business migration sequence applied successfully.
* Focused run: **58 passed, 1 skipped**, including actual PostgreSQL integration.
* Full root suite: **305 passed, 31 skipped, 1 failed, 7 errors**. The failure and
  seven errors are existing encrypted-backup POSIX permission/shebang assumptions
  on Windows; the permission checks were not weakened. A separate path separator
  assumption in the bot-worker test was repaired. Full suite is not green locally.
* Local dependencies: Python 3.11.9, asyncpg 0.32.0, psycopg 3.3.6, Telethon 1.45.0,
  cryptography 46.0.7, pytest 8.4.2, FastAPI 0.162.0, Starlette 1.10.0. `pip check`
  succeeded. Linux PostgreSQL 16 CI on implementation commit `35c7788` completed:
  **313 passed, 31 skipped, 0 failures/errors**, run [41654263499](https://github.com/xe4716-svg/Tgsa/actions/runs/41654263499).
* New tests use no real Telegram client or credentials. No real messages sent.

Full results are retained as JUnit artifacts in CI. Local reports are under
`.local/asd30-*.xml` and remain outside source control. Do not treat skipped
integration suites, fake message IDs or historical imported reports as real
Telegram or production acceptance evidence.


## Durable command trace context (TGM-022)

Migration `018_gateway_command_trace_context.sql` adds a nullable, format-checked
W3C `traceparent` to each durable Gateway command. The trace-aware submit
function wraps the existing authorized/idempotent submit function; it stores only
the first producer parent and preserves it on idempotent retries. The legacy
five-argument submit function is no longer executable by the submitter role.

`GatewayCommandStore.submit_text` injects only the active W3C traceparent.
Baggage, tracestate, message text, peer IDs, tenant/account IDs and request
headers are not persisted as telemetry context. The Gateway claim function
returns the traceparent only to the dedicated executor role. The executor starts
a short consumer span parented to that remote context and records only bounded
operation kind, route generation, outcome, retry delay and sanitized exception
class. The traceparent is excluded from dataclass equality and repr.

Gateway worker tracing is opt-in through
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`; the
worker owns and shuts down its provider. Without an endpoint, tracing is off.

This is a queue-context PoC, not production command-send activation. The current
Gateway worker does not yet install a production command store/transport, and
the current repository has no production caller of `submit_text`. The executor
is validated with a fake sender and the PostgreSQL command integration test.
Do not claim real Telegram command delivery or end-to-end business broadcast
tracing until the AccountBroadcast adapter and production transport are wired
and independently reviewed.

The dedicated PostgreSQL integration now also verifies that a producer span is
persisted and survives an idempotent retry under a different span, then is
returned with the claimed command. It uses disposable database instances and
fake transports only; no Telegram RPC is sent.


## Trace-correlated structured Gateway logs (TGM-022)

The Gateway worker configures an allowlisted JSON logger. The log filter attaches
only the active span's trace ID and span ID. The formatter intentionally ignores
free-form log messages, arguments, exception text and arbitrary extra fields;
it emits only a small event allowlist and bounded command metadata
(operation kind, outcome, route generation, FloodWait retry delay, exception
class). Tenant/account IDs, peer IDs, operation keys, message bodies, session
material, URLs and request headers are never serialized.

Command executor events use fixed event names for completion, FloodWait,
UNKNOWN and sanitized failure. This provides trace-to-log correlation without
turning log messages or user payloads into telemetry attributes. When no active
span exists, the event remains structured but has no trace identifiers.


## Fenced Gateway queue metrics (TGM-022)

Migration `019_gateway_command_metrics.sql` adds a SECURITY DEFINER aggregate
read function for the current Gateway executor. It verifies the executor role,
account route generation, worker ID, fencing token and live lease before
returning only queued/claimed/unknown/flood_wait counts and oldest queued age.
The executor role receives function access, not direct command-table SELECT.

The Gateway worker's OTLP metrics provider is opt-in through
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`.
When enabled, each account executor samples queue state at most once every
15 seconds. Samples are aggregated in memory across accounts and exported
without account/tenant/peer labels. The metrics include bounded command
execution counts/duration, queue age, FloodWait duration, UNKNOWN count,
aggregate queue depth by fixed state and oldest queued age.

Metrics sampling and recording are best effort: telemetry failures do not stop
command dispatch, change command state, or block disconnect cleanup. No
Collector, retention policy or production metrics endpoint is deployed by this
PoC.
