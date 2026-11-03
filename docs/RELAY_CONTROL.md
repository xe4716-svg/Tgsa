# Relay conversation state control

POST /api/v1/relay/conversations/{id}/state accepts status (open, paused, blocked or closed) and expected_generation (positive integer). Idempotency-Key is required, 8–128 ASCII letters/digits or . _ : -. Authentication uses the existing root Bearer session and a current explicit relay.manage business permission. Root VIEWER cannot write.

State, the generation maintained by the existing V2 trigger, the mapped-user audit entry and the successful idempotency response commit together. Stale versions return 409; a conversation outside the mapped tenant returns 404. Same-key/same-command retries replay the original success without another mutation or audit. Same-key/different-command, expired keys and unresolved previous results return 409. Failed transactions do not bind a new key: reload state before retrying a failed command with a newly chosen version. Authorization is rechecked even for replayed responses.

Identity and permission are checked again after idempotency/row-lock waits, before replay or mutation. A role revoked while a command waits on another transaction is rejected without retaining its draft idempotency entry. This is transactional authorization; it does not promise to interrupt a command already committed.

Keys are scoped to the tenant and this operation, retained in the existing V2 idempotency table with a 24-hour expiry; expiry never authorizes automatic reuse. Physical retention cleanup remains a separate work item. Responses and audit metadata contain only identifiers, statuses and generations, never customer Telegram IDs or message contents. This endpoint changes the existing domain state; it does not send Telegram messages or bypass queue generation checks.

Run scripts/configure_relay_control.py with private owner PG* credentials to grant just state updates, idempotency records and audit insertion. No payload, token, password, arbitrary conversation update or DELETE permission is added. bootstrap_combined.py defaults to read-only grants; --enable-relay-control explicitly adds this grant step. The local/staging Compose migration command enables the implemented control module. Existing broad historical grants require their own audit.

Checks include true JWT authentication, current-session/permission revocation, cross-tenant denial, expected-version conflicts, same-key concurrent replay, different-key concurrent version competition, rollback on audit failure and connection context restoration. No real Telegram or payment calls are made.
