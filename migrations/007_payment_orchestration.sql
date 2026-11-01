-- Payment provider configuration, wallet top-up orders and verified webhook inbox.
-- Secrets and private keys stay outside PostgreSQL in the deployment secret manager.
-- Apply after 006_wallet_commerce.sql.

BEGIN;

ALTER TABLE wallet_ledger_entries
    DROP CONSTRAINT IF EXISTS wallet_ledger_entries_entry_type_check;
ALTER TABLE wallet_ledger_entries
    ADD CONSTRAINT wallet_ledger_entries_entry_type_check
    CHECK (entry_type IN (
        'ADMIN_CREDIT', 'ADMIN_DEBIT', 'PURCHASE_DEBIT',
        'REFUND_CREDIT', 'PAYMENT_CREDIT'
    ));
ALTER TABLE wallet_ledger_entries
    DROP CONSTRAINT IF EXISTS wallet_ledger_entries_check;
ALTER TABLE wallet_ledger_entries
    ADD CONSTRAINT wallet_ledger_entries_direction_type_check
    CHECK (
        (direction = 'CREDIT' AND entry_type IN (
            'ADMIN_CREDIT', 'REFUND_CREDIT', 'PAYMENT_CREDIT'
        ))
        OR
        (direction = 'DEBIT' AND entry_type IN (
            'ADMIN_DEBIT', 'PURCHASE_DEBIT'
        ))
    );

CREATE TABLE payment_provider_configs (
    provider VARCHAR(16) PRIMARY KEY
        CHECK (provider IN ('MANUAL', 'STRIPE', 'ALIPAY', 'WECHAT', 'EASYPAY', 'USDT')),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    display_name VARCHAR(80) NOT NULL,
    currencies JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(currencies) = 'array'),
    min_amount_minor BIGINT NOT NULL DEFAULT 100 CHECK (min_amount_minor > 0),
    max_amount_minor BIGINT NOT NULL DEFAULT 100000000
        CHECK (max_amount_minor >= min_amount_minor),
    sort_order INTEGER NOT NULL DEFAULT 100,
    public_instructions TEXT NOT NULL DEFAULT '',
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO payment_provider_configs (
    provider, enabled, display_name, currencies, min_amount_minor,
    max_amount_minor, sort_order, public_instructions
)
VALUES
    ('MANUAL', TRUE, '人工付款', '["CNY","USD","USDT"]'::jsonb, 100, 100000000, 0,
     '请联系平台客服说明充值金额和币种；管理员核实到账后为钱包入账。'),
    ('ALIPAY', FALSE, '支付宝', '["CNY"]'::jsonb, 100, 100000000, 10, ''),
    ('WECHAT', FALSE, '微信支付', '["CNY"]'::jsonb, 100, 100000000, 20, ''),
    ('STRIPE', FALSE, '信用卡', '["USD","CNY"]'::jsonb, 100, 100000000, 30, ''),
    ('EASYPAY', FALSE, '易支付', '["CNY"]'::jsonb, 100, 100000000, 40, ''),
    ('USDT', FALSE, 'USDT (TRC20)', '["USDT"]'::jsonb, 10000000, 100000000000, 50, '')
ON CONFLICT (provider) DO NOTHING;

CREATE TABLE wallet_topup_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_no VARCHAR(48) NOT NULL UNIQUE,
    provider VARCHAR(16) NOT NULL
        REFERENCES payment_provider_configs(provider),
    currency VARCHAR(8) NOT NULL
        CHECK (currency IN ('CNY', 'USD', 'USDT')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PAID', 'EXPIRED', 'FAILED', 'CANCELED')),
    payment_url TEXT,
    qr_code_url TEXT,
    provider_transaction_id VARCHAR(160),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, id)
);
CREATE INDEX idx_wallet_topup_tenant_time
    ON wallet_topup_orders(tenant_id, created_at DESC);
CREATE UNIQUE INDEX uq_wallet_topup_provider_transaction
    ON wallet_topup_orders(provider, provider_transaction_id)
    WHERE provider_transaction_id IS NOT NULL;
CREATE INDEX idx_wallet_topup_pending_expiry
    ON wallet_topup_orders(status, expires_at)
    WHERE status = 'PENDING';

CREATE TABLE payment_webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider VARCHAR(16) NOT NULL
        CHECK (provider IN ('STRIPE', 'ALIPAY', 'WECHAT', 'EASYPAY', 'USDT')),
    event_id VARCHAR(200) NOT NULL,
    order_no VARCHAR(48),
    payload_sha256 CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'RECEIVED'
        CHECK (status IN ('RECEIVED', 'PROCESSED', 'IGNORED', 'FAILED')),
    failure_code VARCHAR(80),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    UNIQUE (provider, event_id)
);
CREATE INDEX idx_payment_webhook_status_time
    ON payment_webhook_events(status, received_at);

ALTER TABLE wallet_topup_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_topup_orders FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON wallet_topup_orders
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Webhooks have no user session. This narrowly-scoped SECURITY DEFINER lookup
-- exposes only a random merchant order's routing and expected amount.
CREATE OR REPLACE FUNCTION resolve_wallet_topup_order(p_order_no TEXT)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    provider VARCHAR,
    currency VARCHAR,
    amount_minor BIGINT,
    status VARCHAR
)
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT o.id, o.tenant_id, o.provider, o.currency, o.amount_minor, o.status
    FROM wallet_topup_orders o
    WHERE o.order_no = p_order_no
    LIMIT 1
$$;
REVOKE ALL ON FUNCTION resolve_wallet_topup_order(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_wallet_topup_order(TEXT) TO tgs_runtime;

GRANT SELECT, INSERT, UPDATE ON payment_provider_configs TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON wallet_topup_orders TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON payment_webhook_events TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON wallet_ledger_entries TO tgs_runtime;
REVOKE UPDATE, DELETE ON wallet_ledger_entries FROM tgs_runtime;

COMMIT;
