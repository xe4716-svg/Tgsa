# Database backup and recovery

Use PostgreSQL client tools matching the server major version. Supply connection credentials through protected PG* environment variables or a password file, never command-line passwords. Backups contain private application data and require protected storage and an explicit retention policy.

Run `python scripts/database_backup.py backup /protected/path/database.dump` with PGDATABASE set to the source database. The destination must not exist. The custom-format backup is published atomically after pg_dump succeeds; failures clean the temporary file. Tool paths can be supplied with --pg-dump.

Run `python scripts/database_backup.py restore /protected/path/database.dump new_database_name`. The target must not exist. createdb creates it, then pg_restore restores in one transaction without --clean. A failed restore leaves the new database for inspection and never drops it. Supply --createdb and --pg-restore when tools are not on PATH.

Roles are cluster-wide and are not in a database dump. Provision the same restricted runtime role separately before restoration; do not rotate its password during recovery. Verify migration versions, table counts, permissions and tenant isolation before redirecting application traffic. Keep the original database until restoration is accepted. These scripts do not provide production storage encryption, scheduled backup monitoring or an automatic traffic cutover.

This backup covers only PostgreSQL. Vault and Session encryption keys are independent secrets and are not included. Formal recovery requires access to the matching key versions in Secret Manager; without them encrypted data cannot be recovered by restoring the database alone.
