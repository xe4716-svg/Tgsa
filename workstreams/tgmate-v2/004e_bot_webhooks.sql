BEGIN;
CREATE TABLE tgmate.bot_webhook_registrations (
 bot_id uuid PRIMARY KEY,
 tenant_id uuid NOT NULL,
 url text NOT NULL,
 secret_hash text NOT NULL CHECK(length(secret_hash)=64),
 secret_ciphertext text NOT NULL,
 status text NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','registering','active','uncertain','disabled')),
 operation_token uuid,
 last_result text,
 last_authenticated_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,bot_id) REFERENCES tgmate.telegram_bots(tenant_id,id)
);
ALTER TABLE tgmate.bot_webhook_registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tgmate.bot_webhook_registrations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tgmate.bot_webhook_registrations USING(tenant_id=tgmate.current_tenant_id()) WITH CHECK(tenant_id=tgmate.current_tenant_id());
COMMIT;
