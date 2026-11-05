# Root SQL migration manifest

The Root migration runner requires `migrations/manifest.json` before it opens a database connection.

The manifest is an append-only release inventory:
- Every `migrations/*.sql` file must appear exactly once.
- Every listed file must exist as a regular file.
- Each entry stores the SHA-256 of the exact UTF-8 file bytes.
- Extra, missing, duplicate, unsafe, or hash-drifted files fail closed before SQL is generated.
- The database ledger still independently rejects already-applied migrations that disappear or change.

## Adding a Root migration

1. Add a new numbered SQL file. Never edit a released migration.
2. Run `python scripts/update_migration_manifest.py` to regenerate the sorted manifest.
3. Review the SQL diff and manifest entry together.
4. Run `pytest -q tests/test_migrations.py tests/test_business_migrations.py`.
5. Let Root integration CI exercise a fresh disposable PostgreSQL database, repeat migration, drift rejection, missing-applied-file rejection, and transaction rollback.

The manifest is not a signature and does not replace PR review; a change to both SQL and its manifest must still be independently reviewed.

## Business migrations

Business migrations keep their separate explicit `FILES` release order in `scripts/migrate_business.py` and the separate `business_schema_migrations` ledger. They must not be added to the Root manifest or Root ledger.
