CREATE EXTENSION IF NOT EXISTS pgcrypto;

\getenv app_db_password APP_DB_PASSWORD

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tgs_runtime') THEN
        CREATE ROLE tgs_runtime LOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

ALTER ROLE tgs_runtime WITH PASSWORD :'app_db_password';

CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(120) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_subject VARCHAR(255) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE tenant_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(30) NOT NULL DEFAULT 'MEMBER'
        CHECK (role IN ('OWNER', 'ADMIN', 'MEMBER', 'VIEWER')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id),
    UNIQUE (tenant_id, id)
);

CREATE TABLE telegram_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    telegram_user_id BIGINT UNIQUE,
    phone_masked VARCHAR(40),
    username VARCHAR(100),
    display_name VARCHAR(160),
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'AUTHORIZED', 'ACTIVE', 'PAUSED', 'REVOKED', 'FROZEN')),
    security_state VARCHAR(30) NOT NULL DEFAULT 'NEW_DEVICE',
    connection_mode VARCHAR(12) NOT NULL DEFAULT 'PROXY'
        CHECK (connection_mode IN ('PROXY', 'DIRECT')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, id)
);
CREATE INDEX idx_telegram_accounts_tenant
    ON telegram_accounts(tenant_id, created_at DESC);

CREATE TABLE telegram_sessions (
    account_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    ciphertext BYTEA NOT NULL,
    nonce BYTEA NOT NULL,
    wrapped_dek BYTEA NOT NULL,
    kms_key_id VARCHAR(255) NOT NULL,
    encryption_version SMALLINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES telegram_accounts(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE proxy_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proxy_type VARCHAR(20) NOT NULL
        CHECK (proxy_type IN ('SOCKS5', 'MTProto', 'HTTP_CONNECT')),
    host VARCHAR(255) NOT NULL,
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    credentials_ciphertext BYTEA,
    secret_version VARCHAR(80),
    claimed_sharing_mode VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (claimed_sharing_mode IN ('EXCLUSIVE', 'SHARED', 'UNKNOWN')),
    verified_sharing_mode VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (verified_sharing_mode IN ('EXCLUSIVE', 'SHARED', 'UNKNOWN')),
    verified_egress_ip INET,
    verification_status VARCHAR(20) NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (verification_status IN ('UNVERIFIED', 'PENDING', 'VERIFIED', 'FAILED')),
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'DISABLED', 'FAILED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, id)
);
CREATE INDEX idx_proxy_profiles_tenant
    ON proxy_profiles(tenant_id, created_at DESC);

CREATE UNIQUE INDEX uq_verified_exclusive_egress_ip
    ON proxy_profiles(verified_egress_ip)
    WHERE verified_sharing_mode = 'EXCLUSIVE'
      AND verification_status = 'VERIFIED'
      AND status = 'ACTIVE'
      AND verified_egress_ip IS NOT NULL;

CREATE TABLE account_proxy_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    account_id UUID NOT NULL,
    proxy_profile_id UUID NOT NULL,
    allocation_mode VARCHAR(16) NOT NULL
        CHECK (allocation_mode IN ('EXCLUSIVE', 'SHARED')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES telegram_accounts(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, proxy_profile_id)
        REFERENCES proxy_profiles(tenant_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_account_one_active_proxy
    ON account_proxy_bindings(account_id) WHERE is_active = TRUE;
CREATE UNIQUE INDEX uq_proxy_one_exclusive_account
    ON account_proxy_bindings(proxy_profile_id)
    WHERE is_active = TRUE AND allocation_mode = 'EXCLUSIVE';

CREATE TABLE connection_consents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    connection_mode VARCHAR(12) NOT NULL
        CHECK (connection_mode IN ('PROXY', 'DIRECT')),
    proxy_profile_id UUID,
    notice_version VARCHAR(30) NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, proxy_profile_id)
        REFERENCES proxy_profiles(tenant_id, id),
    CHECK (
        (connection_mode = 'PROXY' AND proxy_profile_id IS NOT NULL)
        OR (connection_mode = 'DIRECT' AND proxy_profile_id IS NULL)
    )
);

CREATE TABLE account_gateway_leases (
    account_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    worker_id VARCHAR(150) NOT NULL,
    fencing_token BIGINT NOT NULL DEFAULT 1,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES telegram_accounts(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE account_security_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    account_id UUID NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    severity VARCHAR(16) NOT NULL
        CHECK (severity IN ('INFO', 'WARNING', 'HIGH', 'CRITICAL')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES telegram_accounts(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_security_events_account_time
    ON account_security_events(account_id, created_at DESC);

CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id UUID REFERENCES users(id),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(80) NOT NULL,
    resource_id UUID,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_logs_tenant_time
    ON audit_logs(tenant_id, created_at DESC);

GRANT CONNECT ON DATABASE tgs TO tgs_runtime;
GRANT USAGE ON SCHEMA public TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tgs_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tgs_runtime;
