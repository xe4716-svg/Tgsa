BEGIN;
CREATE TABLE tgmate.relay_operator_authorizations (
 tenant_id uuid NOT NULL,
 config_id uuid NOT NULL,
 operator_user_id bigint NOT NULL CHECK(operator_user_id>0),
 challenge_hash text NOT NULL CHECK(length(challenge_hash)=64),
 expires_at timestamptz NOT NULL,
 confirmed_at timestamptz,
 requested_by uuid NOT NULL REFERENCES tgmate.users(id),
 requested_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,config_id),
 UNIQUE(challenge_hash),
 FOREIGN KEY(tenant_id,config_id) REFERENCES tgmate.relay_configs(tenant_id,id)
);
ALTER TABLE tgmate.relay_operator_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tgmate.relay_operator_authorizations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tgmate.relay_operator_authorizations USING(tenant_id=tgmate.current_tenant_id()) WITH CHECK(tenant_id=tgmate.current_tenant_id());
COMMIT;
