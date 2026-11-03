# Explicit identity bridge

Root authentication remains the only entry point. Operators provision explicit tenant, user and membership mappings offline; emails never create a mapping. The bridge rechecks the root active session and membership, then checks the mapped business membership and exact permission. Root VIEWER cannot request write permissions, and tenant.admin is never promoted through the bridge.

Migration 010 stores UUID mappings without requiring the business schema during root-only deployment. Resolution requires the actual tgmate schema and fails closed if unavailable. Runtime grants on mappings are SELECT only. Business permission reads occur on the same connection under a savepoint, restoring the root tenant context afterwards. This does not attach V2 write APIs or migrate existing users automatically.

Removing a root membership cascades only its member mapping, allowing immediate access revocation without deleting V2 business membership or content. Root tenant/user deletion also removes the corresponding mappings. Linking and runtime-column grants are documented in IDENTITY_LINKING.md.

Opt-in test: TGMATE_IDENTITY_INTEGRATION=1 with owner PG* and optional PG_PSQL. It creates its own tgs_identity_ child database and applies original root and V2 migrations, independent of the caller's database. Use only a dedicated disposable test cluster. Tests restore the existing tgs_runtime password hash; child test databases are retained for inspection. No Telegram or payment requests are sent.
