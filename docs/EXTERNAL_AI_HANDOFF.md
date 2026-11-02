# External AI collaboration handoff

Repository: xe4716-svg/Tgsa. Current shared implementation base: codex/identity-integration (PR #20), stacked on codex/worker-recovery (PR #19). main and integration/mvp do not yet contain these changes. Pull current PR state before starting; do not overwrite another branch or assume a merged baseline.

## Work already owned by the coordinator

Identity/tenant mappings, root migration 010, app/integration, bridge tests and shared CI changes are in progress. Do not modify these concurrently. Existing migrations are immutable. Coordinate new migration numbers and shared contracts through a PR before changing them.

## Independent work packages

| Suggested branch | Scope and owned paths | Acceptance |
| --- | --- | --- |
| codex/external-channel-clone | app/cloning/, tests/test_cloning*, docs/CHANNEL_CLONE.md | Authorized source/target checks; realtime buffer before history; durable mappings; resumable progress; deduplication; edits/deletes and media-group boundaries; pause/cancel and FloodWait/unknown-result tests. |
| codex/external-sticker-clone | app/stickers/, tests/test_stickers*, docs/STICKER_CLONE.md | Supported-type preflight, source authorization, idempotent jobs, progress and retry, damaged/unsupported material tests. Coordinate shared media contract before reusing clone internals. |
| codex/external-console | frontend/, docs/CONSOLE.md | Responsive customer/admin interface connected to approved APIs; separate bot/account broadcast entries; loading/failure/idempotency UX; no Session/token/API Hash persistence. Browser and build evidence required. Unimplemented APIs must be explicitly marked, never reported as working. |

Each collaborator should inspect existing root and V2 implementations before coding, reuse identity/billing/queue capabilities, and submit one focused draft PR. Target the agreed integration branch; use the current implementation branch as a stacked base when necessary and describe that dependency. Do not merge main, deploy, configure production credentials or modify other owners' files.

## Delivery requirements

Record the concrete behavior, changed files, test command/results and unverified external conditions. Distinguish simulated external adapters from real PostgreSQL checks and real Telegram/payment integrations. Respect existing authorization, opt-in requirements, protected content and platform rate limits. Never silently switch sender identity or blindly resend an uncertain delivery. No production credentials or account Session material in chats, source, tests or logs.

The four V1.2 groups remain platform, Bot business, account/clone, and frontend/commercial, coordinated centrally. Available agents may implement several subpackages within these groups; branch existence is not evidence that an external AI is connected or running.
