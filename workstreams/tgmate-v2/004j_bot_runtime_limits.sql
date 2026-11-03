BEGIN;
CREATE TABLE tgmate.bot_runtime_limits (
 tenant_id uuid NOT NULL,
 bot_id uuid NOT NULL,
 wait_until timestamptz NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,bot_id),
 FOREIGN KEY(tenant_id,bot_id) REFERENCES tgmate.telegram_bots(tenant_id,id) ON DELETE CASCADE
);
ALTER TABLE tgmate.bot_runtime_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE tgmate.bot_runtime_limits FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tgmate.bot_runtime_limits
 USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
 WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
COMMIT;
