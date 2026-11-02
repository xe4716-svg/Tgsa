# Combined schema initialization

Use a dedicated database and migration owner; supply PG* credentials privately.

`python scripts/bootstrap_combined.py` runs both migration streams and configures identity/relay metadata read grants in order. It stops on the first failure and does not auto-link identities. Each committed migration remains recorded for a safe retry.

For a new local Compose installation, copy `.env.example` to `.env` for the migration-owner settings, and `.env.runtime.example` to `.env.runtime` for API/worker settings. Match the runtime DATABASE_URL password to APP_DB_PASSWORD. Keep the database administrator password out of `.env.runtime`. Generate JWT/Session keys for that runtime file. `docker compose up --build` runs the one-shot migrate service before API/billing. The database and Redis remain internal; HTTP/Mailpit bind localhost. Historical volumes without ledgers are refused rather than overwritten. This Compose setup is for local/staging rehearsal and does not supply production HTTPS, SMTP or a secret manager.

CI runs `python3 scripts/container_smoke.py` in an isolated Compose project. It refuses an existing `.env.runtime`, generates temporary test configuration, verifies service readiness and both ledgers, replays migrations and checks unauthenticated API rejection. It removes only its generated runtime file and its own project volumes. Local Windows validation does not claim this container rehearsal passed; the remote workflow is the evidence for that step.

1. Run `python scripts/migrate.py` to apply root migrations through 010.
2. Run `python scripts/migrate_business.py` to apply the immutable V2 release in dependency order.
3. Provision explicit identity links and column-level runtime grants with `scripts/link_identity.py`, following IDENTITY_LINKING.md.

The two streams have separate ledgers (`public.schema_migrations` and `public.business_schema_migrations`) and advisory locks. Business ledger names include an ordering prefix and original filename; hashes are of the original SQL bytes. Every file and its ledger entry commit together. Earlier files remain committed if a later file fails; rerun safely after resolving the failure. Applied-file drift or missing required release files is rejected. The original SQL is never edited by the runner.

An existing tgmate schema without a tracked business ledger is refused, including an empty ledger alongside that schema. There is no automatic adoption of a historical database. Back up and review its schema/data before designing an explicit adoption procedure. Do not fabricate ledger rows to bypass this check.

The explicit inspection/adoption procedure and narrowly recognized historical reconciliation are documented in LEGACY_SCHEMA_ADOPTION.md. They remain separate from normal startup and never auto-run against a historical volume.

Root-only deployments remain supported; the business runner requires root identity migration 010. These commands do not migrate identities by email, enable a second login service, grant business write privileges, or start Telegram workers. Session/Vault keys remain separate from database initialization.

Tests cover script composition, release order, isolated ledgers, missing source rejection and sanitized failures. Set `TGMATE_BUSINESS_MIGRATION_INTEGRATION=1` with private owner PG* and optional PG_PSQL to run the disposable-child-database test. This requires a dedicated test cluster. Local actual PostgreSQL validation completed with 5 tests passing in 24.56s: first/repeat execution, all 12 original SQL hashes, separate root ledger count 10, hash drift and untracked/missing-root refusal, failed-file rollback and successful resume. Evidence is in docs/validation/business-migrations.json. This is database validation, not Telegram, payment or production deployment acceptance.
