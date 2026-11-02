# Unified customer-relay metadata reads

GET /api/v1/relay/conversations uses the root bearer-token authentication, explicit identity mapping and current V2 relay.read permission. The request cannot select another tenant. Cursor pagination orders created_at/id, defaults to 20 and caps at 100. Responses contain only conversation metadata; Telegram customer/operator IDs, payloads and credentials are excluded.

Provision root/V2 schemas in one database, link identities with scripts/link_identity.py, configure its identity column grants, then run scripts/configure_business_reads.py for the runtime role. The additional tool grants only the metadata columns used by this endpoint. No customer User ID, payload ciphertext or business write grants are supplied.

The real database test creates an isolated child database and restricted login role. It exercises a signed JWT through the actual root authentication dependency, identity resolver and API SQL. It checks two-tenant data isolation, equal-timestamp cursor pagination, forged tenant query parameters, withdrawn business permission/mapping, and revoked root session. No authentication dependency override is used for this path. No Telegram requests are sent.

Opt in with TGMATE_BUSINESS_READS_INTEGRATION=1 and owner PG* on a disposable test cluster; PG_PSQL selects the psql executable. Other migration/resource/identity integration tests have separate opt-in flags. Evidence is in validation/business-reads.json and XML; these are local execution results, not production deployment or Telegram delivery acceptance.
