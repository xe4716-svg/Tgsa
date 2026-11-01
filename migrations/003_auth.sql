ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email VARCHAR(254),
    ADD COLUMN IF NOT EXISTS password_hash TEXT,
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS default_tenant_id UUID;

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_status_check;

ALTER TABLE users
    ADD CONSTRAINT users_status_check
    CHECK (status IN ('ACTIVE', 'DISABLED', 'PENDING_VERIFICATION'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower
    ON users (lower(email))
    WHERE email IS NOT NULL;

ALTER TABLE users
    ADD CONSTRAINT fk_users_default_tenant
    FOREIGN KEY (default_tenant_id) REFERENCES tenants(id) ON DELETE SET NULL;

CREATE TABLE email_verification_tokens (
    token_hash CHAR(64) PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_email_verification_user
    ON email_verification_tokens(tenant_id, user_id, created_at DESC);

CREATE TABLE password_reset_tokens (
    token_hash CHAR(64) PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_password_reset_user
    ON password_reset_tokens(tenant_id, user_id, created_at DESC);

CREATE TABLE auth_refresh_tokens (
    token_hash CHAR(64) PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    replaced_by_hash CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_refresh_family
    ON auth_refresh_tokens(tenant_id, family_id);
CREATE INDEX idx_refresh_user
    ON auth_refresh_tokens(tenant_id, user_id, created_at DESC);

ALTER TABLE email_verification_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_verification_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON email_verification_tokens
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE password_reset_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON password_reset_tokens
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE auth_refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_refresh_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON auth_refresh_tokens
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON email_verification_tokens, password_reset_tokens, auth_refresh_tokens
    TO tgs_runtime;


-- Password reset must invalidate refresh sessions in every workspace.
-- The function is SECURITY DEFINER so tenant RLS does not leave old sessions alive
-- in a second workspace. It accepts only a user UUID, has a fixed search_path,
-- and is executable only by the application database role.
CREATE OR REPLACE FUNCTION revoke_all_user_refresh_tokens(target_user_id UUID)
RETURNS VOID
LANGUAGE SQL
SECURITY DEFINER
SET search_path = public
AS $$
    UPDATE auth_refresh_tokens
    SET revoked_at = COALESCE(revoked_at, NOW())
    WHERE user_id = target_user_id;
$$;

REVOKE ALL ON FUNCTION revoke_all_user_refresh_tokens(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION revoke_all_user_refresh_tokens(UUID) TO tgs_runtime;
