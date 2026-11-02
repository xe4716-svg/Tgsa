# TGMate implementation checkpoint

Source baseline: d5770627c6e7e2b0e60260f0a036f9604c2d26a9 plus the health-probe fix in PR #17.
V1.2 context recovered from the existing “整合规划进度” chat: modular monolith; API, Bot and MTProto processes; PostgreSQL and Redis; four specialist groups and one coordinator; no predetermined cloud provider; Telegram internal forwarding/media reuse for authorized clone operations.

## Code delivered in this work package

- Versioned root-schema migration runner and reproducible database lifecycle tests.
- Resource cleanup on partial startup and worker failure.
- Authenticated tenant-scoped account/proxy read APIs with cursor pagination and v1 response envelopes.
- Root integration workflow with disposable PostgreSQL, plus local real-database evidence.
- Separate commercial worker entrypoint and Compose service; API background work now defaults off.
- Refuse-overwrite backups and restore-to-new-database tooling, with an actual restore and RLS rehearsal.
- Explicit root/V2 identity mappings, current-session and permission checks, and offline linking/column-grant tools; verified against both schemas in one database.
- Root-authenticated relay metadata API, separate tracked business migrations, and ordered combined initialization for Compose.
- Version-checked relay conversation state controls with same-transaction audits and successful-request idempotency; no Telegram send is triggered.
- Separate root-authenticated Bot broadcast, account broadcast and moderation metadata inventories, with independent channel read permissions.

The new read routes are /api/v1/telegram/accounts and /api/v1/proxies. They expose fixed metadata columns, never encrypted sessions or proxy credentials. They do not authorize account login, proxy changes, or message sending.

## Remaining acceptance work

| Area | Current boundary / work remaining |
| --- | --- |
| Identity and database unification | Explicit mapping and read-only permission bridge delivered; root and V2 schemas verified in one database. Business API wiring, existing-data migration and full unified deployment remain. Never match identities by email alone. |
| Bot/group moderation/customer relay | Root-authenticated customer-relay metadata reads and conversation state controls delivered. Remaining relay/config/privacy/operator APIs, complete console, moderation/broadcast integration and persistent Telegram runtime remain. |
| Bot and account broadcast | Preserve separate identities, endpoints, queues, budgets, quotas, rate policies and statistics. Root integration remains. |
| Account authorization and proxies | Existing login/session/proxy components plus new metadata reads; gateway ownership, controlled writes and full UI remain. |
| Channel/group clone | Historical/realtime handoff, durable mapping, edit/delete, media groups and authorized Telegram integration remain. |
| Sticker/emoji clone | Type validation, job runtime, retries, progress and Telegram integration remain. |
| Commercial features | Existing membership/payment/wallet/notification implementations must be retained; dual-channel entitlements, quota audit, commission and sandbox reconciliation remain. |
| Customer/admin frontend | Full responsive console, admin RBAC, tutorial center and browser acceptance remain. |
| Long-term modules | CRM, analytics, asset library, diagnostics, teams, API/webhooks, workflows, AI operations/customer service/knowledge base and advanced commercial operations remain in scope. |
| Operations | API/commercial worker separation and root-database restore rehearsal delivered. Full Bot/MTProto runtime separation, retention cleanup, key recovery, monitoring and target-environment deployment/fault testing remain. |
| External acceptance | Real Telegram authorization, payment sandbox, HTTPS/staging and production credential rotation are unverified. |

No main merge, deployment or final acceptance is claimed. Tests involving simulated external services are distinct from actual PostgreSQL migration/RLS checks.
