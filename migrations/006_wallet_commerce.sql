-- Wallet, price snapshots and paid subscription terms.
-- Currency amounts are integer minor units: CNY/USD use cents; USDT uses 1e-6 USDT.
-- Apply after 005_entitlement_overrides.sql.

BEGIN;

ALTER TABLE tenant_subscriptions
    DROP CONSTRAINT IF EXISTS tenant_subscriptions_status_check;
ALTER TABLE tenant_subscriptions
    ADD CONSTRAINT tenant_subscriptions_status_check
    CHECK (status IN ('TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCELED', 'EXPIRED', 'SUPERSEDED'));

ALTER TABLE tenant_subscriptions
    DROP CONSTRAINT IF EXISTS tenant_subscriptions_billing_source_check;
ALTER TABLE tenant_subscriptions
    ADD CONSTRAINT tenant_subscriptions_billing_source_check
    CHECK (billing_source IN ('MANUAL', 'PAYMENT_PROVIDER', 'PROMOTION', 'WALLET'));

CREATE TABLE membership_plan_prices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES membership_plans(id),
    currency VARCHAR(8) NOT NULL
        CHECK (currency ~ '^[A-Z][A-Z0-9]{2,7}$'),
    duration_days INTEGER NOT NULL CHECK (duration_days BETWEEN 1 AND 3650),
    amount_minor BIGINT NOT NULL CHECK (amount_minor >= 0),
    version INTEGER NOT NULL CHECK (version > 0),
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_id, currency, duration_days, version)
);
CREATE UNIQUE INDEX uq_current_plan_price
    ON membership_plan_prices(plan_id, currency, duration_days)
    WHERE is_current = TRUE;
CREATE INDEX idx_plan_prices_public
    ON membership_plan_prices(currency, duration_days, plan_id)
    WHERE is_current = TRUE;

CREATE TABLE tenant_wallet_accounts (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    currency VARCHAR(8) NOT NULL
        CHECK (currency ~ '^[A-Z][A-Z0-9]{2,7}$'),
    balance_minor BIGINT NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, currency)
);

CREATE TABLE billing_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_no VARCHAR(48) NOT NULL UNIQUE,
    order_kind VARCHAR(16) NOT NULL
        CHECK (order_kind IN ('PURCHASE', 'RENEWAL', 'UPGRADE')),
    plan_id UUID NOT NULL REFERENCES membership_plans(id),
    plan_code_snapshot VARCHAR(40) NOT NULL,
    plan_name_snapshot VARCHAR(80) NOT NULL,
    duration_days INTEGER NOT NULL CHECK (duration_days BETWEEN 1 AND 3650),
    currency VARCHAR(8) NOT NULL
        CHECK (currency ~ '^[A-Z][A-Z0-9]{2,7}$'),
    list_amount_minor BIGINT NOT NULL CHECK (list_amount_minor >= 0),
    upgrade_credit_minor BIGINT NOT NULL DEFAULT 0
        CHECK (upgrade_credit_minor >= 0),
    payable_amount_minor BIGINT NOT NULL CHECK (payable_amount_minor >= 0),
    price_version INTEGER NOT NULL CHECK (price_version > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'PAID'
        CHECK (status IN ('PENDING', 'PAID', 'CANCELED', 'REFUNDED', 'FAILED')),
    payment_source VARCHAR(16) NOT NULL DEFAULT 'WALLET'
        CHECK (payment_source IN ('WALLET', 'MANUAL', 'ALIPAY', 'WECHAT', 'CARD', 'USDT', 'EASYPAY')),
    idempotency_key VARCHAR(120) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    paid_at TIMESTAMPTZ,
    effective_start_at TIMESTAMPTZ,
    effective_end_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, id),
    CHECK (upgrade_credit_minor <= list_amount_minor),
    CHECK (payable_amount_minor = list_amount_minor - upgrade_credit_minor)
);
CREATE INDEX idx_billing_orders_tenant_time
    ON billing_orders(tenant_id, created_at DESC);

CREATE TABLE wallet_ledger_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    currency VARCHAR(8) NOT NULL
        CHECK (currency ~ '^[A-Z][A-Z0-9]{2,7}$'),
    direction VARCHAR(8) NOT NULL CHECK (direction IN ('CREDIT', 'DEBIT')),
    entry_type VARCHAR(24) NOT NULL
        CHECK (entry_type IN ('ADMIN_CREDIT', 'ADMIN_DEBIT', 'PURCHASE_DEBIT', 'REFUND_CREDIT')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    balance_after_minor BIGINT NOT NULL CHECK (balance_after_minor >= 0),
    reference_type VARCHAR(40),
    reference_id UUID,
    idempotency_key VARCHAR(120) NOT NULL,
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    reason VARCHAR(500) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, idempotency_key),
    CHECK (
        (direction = 'CREDIT' AND entry_type IN ('ADMIN_CREDIT', 'REFUND_CREDIT'))
        OR (direction = 'DEBIT' AND entry_type IN ('ADMIN_DEBIT', 'PURCHASE_DEBIT'))
    )
);
CREATE INDEX idx_wallet_ledger_tenant_time
    ON wallet_ledger_entries(tenant_id, created_at DESC);

CREATE TABLE subscription_terms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_id UUID NOT NULL UNIQUE,
    plan_id UUID NOT NULL REFERENCES membership_plans(id),
    plan_code_snapshot VARCHAR(40) NOT NULL,
    currency VARCHAR(8) NOT NULL
        CHECK (currency ~ '^[A-Z][A-Z0-9]{2,7}$'),
    duration_days INTEGER NOT NULL CHECK (duration_days BETWEEN 1 AND 3650),
    gross_amount_minor BIGINT NOT NULL CHECK (gross_amount_minor >= 0),
    upgrade_credit_minor BIGINT NOT NULL DEFAULT 0
        CHECK (upgrade_credit_minor >= 0),
    net_paid_minor BIGINT NOT NULL CHECK (net_paid_minor >= 0),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'EXPIRED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (ends_at > starts_at),
    UNIQUE (tenant_id, id),
    CHECK (upgrade_credit_minor <= gross_amount_minor),
    CHECK (net_paid_minor = gross_amount_minor - upgrade_credit_minor),
    FOREIGN KEY (tenant_id, order_id)
        REFERENCES billing_orders(tenant_id, id)
);
CREATE INDEX idx_subscription_terms_remaining
    ON subscription_terms(tenant_id, status, ends_at);

CREATE TABLE platform_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(80) NOT NULL,
    resource_id UUID,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_platform_audit_time ON platform_audit_logs(created_at DESC);

-- Tenant-owned commerce records are isolated using the same transaction-local tenant context.
ALTER TABLE tenant_wallet_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_wallet_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_wallet_accounts
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE billing_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_orders FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON billing_orders
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE wallet_ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_ledger_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON wallet_ledger_entries
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE subscription_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscription_terms FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON subscription_terms
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT ON membership_plan_prices TO tgs_runtime;
GRANT INSERT, UPDATE ON membership_plan_prices TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON tenant_wallet_accounts TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON billing_orders TO tgs_runtime;
GRANT SELECT, INSERT ON wallet_ledger_entries TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON subscription_terms TO tgs_runtime;
GRANT SELECT, INSERT ON platform_audit_logs TO tgs_runtime;
REVOKE UPDATE, DELETE ON wallet_ledger_entries FROM tgs_runtime;

COMMIT;
