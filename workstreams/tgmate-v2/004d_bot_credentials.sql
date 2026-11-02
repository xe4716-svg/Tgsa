-- Global unique external identity with tenant-visible rows only; no ownership transfer API.
BEGIN;
CREATE TABLE tgmate.bot_ownership_claims (
 external_bot_id bigint PRIMARY KEY CHECK(external_bot_id>0),
 tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
 bot_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,bot_id),
 FOREIGN KEY(tenant_id,bot_id) REFERENCES tgmate.telegram_bots(tenant_id,id)
);
CREATE TABLE tgmate.bot_credentials (
 tenant_id uuid NOT NULL,
 bot_id uuid NOT NULL,
 token_ciphertext text NOT NULL,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,bot_id),
 FOREIGN KEY(tenant_id,bot_id) REFERENCES tgmate.telegram_bots(tenant_id,id)
);
ALTER TABLE tgmate.bot_ownership_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE tgmate.bot_ownership_claims FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tgmate.bot_ownership_claims USING(tenant_id=tgmate.current_tenant_id()) WITH CHECK(tenant_id=tgmate.current_tenant_id());
ALTER TABLE tgmate.bot_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE tgmate.bot_credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tgmate.bot_credentials USING(tenant_id=tgmate.current_tenant_id()) WITH CHECK(tenant_id=tgmate.current_tenant_id());
COMMIT;
