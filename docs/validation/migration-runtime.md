# Migration and runtime validation

The root schema now has a psql migration runner: `python scripts/migrate.py`.
Use a dedicated migration owner with PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD supplied privately. APP_DB_PASSWORD initializes the existing tgs_runtime role on first application; this role is cluster-wide, so use a dedicated test cluster or coordinate its credentials before initializing another database.

The runner records original file SHA-256 hashes, holds one advisory lock for the session, checks the complete applied ledger before new migrations, and commits each file with its ledger entry. Previous successful files remain committed after a later file fails. Existing untracked schemas are not automatically adopted. Do not run the old Docker entrypoint SQL initialization and then treat this runner as an upgrade tool without a reviewed baseline adoption.

Released SQL is unchanged. The runner accepts the current released transaction wrapper format and converts only the exact foundation CONNECT grant to the connected database name. It is not a parser for arbitrary transaction-control SQL. Future migrations must use the supported format and be reviewed.

Local PostgreSQL 17.11 validation is recorded in migration-runner.json: fresh 001–009, repeat execution, missing files, hash drift, and failed-file rollback. Opt-in reusable integration test:
`TGMATE_MIGRATION_INTEGRATION=1 python -m pytest tests/test_migrations.py -q`.
This requires a fresh disposable database. PG_PSQL optionally supplies the executable path.

The root-integration workflow runs the tests against its own PostgreSQL service. Adding the workflow is not evidence of a completed remote CI run.

Runtime cleanup now signals both workers before waiting, and closes Redis/PostgreSQL even if startup or a worker fails. These paths are covered by simulated lifecycle tests. This does not yet separate API/Bot/MTProto processes or prove Telegram/payment delivery.
