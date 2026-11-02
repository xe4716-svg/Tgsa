# Explicit identity linking

Apply root migration 010 and the V2 schema to the same dedicated database first. Identity links are provisioned by a migration owner, never by matching email addresses or accepting client tenant IDs.

Supply private PGHOST, PGPORT, PGDATABASE, PGUSER and PGPASSWORD. Run `python scripts/link_identity.py --tenant ROOT_UUID --user ROOT_USER_UUID --business-tenant BUSINESS_UUID --business-user BUSINESS_USER_UUID` to validate active membership and existing mapping compatibility without writing links. Add `--apply` to atomically create consistent links; repeated identical links are accepted and conflicting links are rejected. The script locks the mapping tables during its checks and writes.

Run the separate explicit operation `python scripts/link_identity.py --configure-runtime-read` to grant the existing runtime role only the V2 identity/status and role-permission columns needed by the bridge. The role must not be superuser, bypass RLS, or own public/V2 relations. This operation requires the V2 schema; it does not grant passwords, sessions, credentials or business writes. It does not revoke previously provisioned privileges: existing deployments require a separate privilege audit.

The application bridge rechecks root session and membership plus current V2 membership and exact permissions on every resolution. Missing maps and unavailable schema fail closed. Provisioning a link does not activate a second login system or authorize business API writes.
