# Independent business inventories

Read-only endpoints:
- /api/v1/broadcast/bot-tasks requires bot_broadcast.read.
- /api/v1/broadcast/account-tasks requires account_broadcast.read.
- /api/v1/moderation/groups requires moderation.read.

Each endpoint authenticates the current root session and resolves the explicit business identity. Server-selected module/table filters and tenant RLS apply; request tenant_id, sender_type and permission parameters cannot change the selected channel. Metadata uses a fixed column list and (created_at,id) cursor pagination, default 20/max 100.

Task payloads, idempotency keys, moderation welcome text, credentials, sessions and raw Telegram peer IDs are not returned or granted to these inventory queries. List status is the stored task-registry status, not a derived delivery statistic. This work does not implement task creation, worker sends or paid quota consumption.

scripts/configure_catalog_reads.py registers the two independent broadcast read codes and grants metadata columns to a checked non-owner/non-bypass runtime role. It never assigns permissions to member roles. Migration owners must explicitly assign approved codes to the mapped V2 role. The historical broadcast.read permission is not silently expanded into both new permissions. The existing legacy module remains unchanged.

Combined initialization includes the read configuration step. Current root VIEWER may read only when its mapped business role has the specific read permission. Expired or revoked sessions are rejected. Column grants do not revoke wider historical grants; existing environments still require privilege review.

Tests include real JWT authentication, two tenant datasets, separate bot/account task tables, group metadata, same-timestamp pagination, per-channel grant/revoke behavior and sensitive-column rejection. No Telegram or payment calls are made.
