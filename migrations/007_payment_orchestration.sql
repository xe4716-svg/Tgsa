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
    ('USDT', FALSE, 'USDT (TRC20)', '["USDT"]'::jsonb, 1000000, 100000000000, 50, '')
ON CONFLICT (provider) DO NOTHING;

CREATE TABLE wallet_topup_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_no VARCHAR(48) NOT NULL UNIQUE,
    provider VARCHAR(16) NOT NULL
        REFERENCES payment_provider_configs(provider),
    idempotency_key VARCHAR(120) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
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
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX idx_wallet_topup_tenant_time
    ON wallet_topup_orders(tenant_id, created_at DESC);
CREATE UNIQUE INDEX uq_wallet_topup_provider_transaction
    ON wallet_topup_orders(provider, provider_transaction_id)
    WHERE provider_transaction_id IS NOT NULL;
CREATE INDEX idx_wallet_topup_pending_expiry
    ON wallet_topup_orders(status, expires_at)
    WHERE status = 'PENDING';

ALTER TABLE wallet_topup_orders
    ADD COLUMN deposit_address VARCHAR(64),
    ADD COLUMN derivation_index BIGINT,
    ADD COLUMN last_scanned_at TIMESTAMPTZ;

ALTER TABLE wallet_topup_orders
    DROP CONSTRAINT IF EXISTS wallet_topup_orders_status_check;
ALTER TABLE wallet_topup_orders
    ADD CONSTRAINT wallet_topup_orders_status_check
    CHECK (status IN ('PENDING', 'PAID', 'EXPIRED', 'FAILED', 'CANCELED', 'REVIEW'));

CREATE UNIQUE INDEX uq_wallet_topup_deposit_address
    ON wallet_topup_orders(deposit_address)
    WHERE deposit_address IS NOT NULL;
CREATE UNIQUE INDEX uq_wallet_topup_derivation_index
    ON wallet_topup_orders(derivation_index)
    WHERE derivation_index IS NOT NULL;
CREATE INDEX idx_tron_deposits_due
    ON wallet_topup_orders(status, last_scanned_at, created_at)
    WHERE provider = 'USDT' AND deposit_address IS NOT NULL;

CREATE TABLE tron_sweep_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    topup_order_id UUID NOT NULL,
    source_address VARCHAR(64) NOT NULL,
    derivation_index BIGINT NOT NULL CHECK (derivation_index >= 0),
    destination_address VARCHAR(64) NOT NULL,
    token_contract VARCHAR(64) NOT NULL,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    max_fee_sun BIGINT NOT NULL CHECK (max_fee_sun >= 0),
    fee_sun BIGINT CHECK (fee_sun >= 0),
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN (
            'PENDING', 'PROCESSING', 'RETRY', 'SUBMITTED',
            'CONFIRMED', 'FAILED', 'REVIEW'
        )),
    txid VARCHAR(64),
    signer_reference VARCHAR(160),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    last_error_code VARCHAR(80),
    requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
    reason VARCHAR(500) NOT NULL DEFAULT 'Automatic TRC20 consolidation',
    submitted_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, topup_order_id),
    FOREIGN KEY (tenant_id, topup_order_id)
        REFERENCES wallet_topup_orders(tenant_id, id)
);
CREATE UNIQUE INDEX uq_tron_sweep_txid
    ON tron_sweep_jobs(txid)
    WHERE txid IS NOT NULL;
CREATE INDEX idx_tron_sweep_due
    ON tron_sweep_jobs(status, next_attempt_at, created_at)
    WHERE status IN ('PENDING', 'RETRY');
CREATE INDEX idx_tron_sweep_confirmation
    ON tron_sweep_jobs(status, last_checked_at)
    WHERE status = 'SUBMITTED';

ALTER TABLE tron_sweep_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE tron_sweep_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tron_sweep_jobs
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE OR REPLACE FUNCTION claim_pending_tron_sweeps(p_limit INTEGER DEFAULT 10)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    topup_order_id UUID,
    source_address VARCHAR,
    derivation_index BIGINT,
    destination_address VARCHAR,
    token_contract VARCHAR,
    amount_minor BIGINT,
    max_fee_sun BIGINT,
    attempt_count INTEGER
)
LANGUAGE SQL
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $sweep$
    WITH due AS (
        SELECT j.id
        FROM tron_sweep_jobs j
        WHERE (
              (j.status IN ('PENDING', 'RETRY') AND j.next_attempt_at <= NOW())
              OR (j.status = 'PROCESSING' AND j.updated_at < NOW() - INTERVAL '5 minutes')
          )
        ORDER BY j.next_attempt_at, j.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 10), 1), 100)
    )
    UPDATE tron_sweep_jobs j
    SET status = 'PROCESSING',
        attempt_count = j.attempt_count + 1,
        updated_at = NOW()
    FROM due
    WHERE j.id = due.id
    RETURNING j.id, j.tenant_id, j.topup_order_id, j.source_address,
              j.derivation_index, j.destination_address, j.token_contract,
              j.amount_minor, j.max_fee_sun, j.attempt_count
$sweep$;

CREATE OR REPLACE FUNCTION claim_submitted_tron_sweeps(p_limit INTEGER DEFAULT 20)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    txid VARCHAR
)
LANGUAGE SQL
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $confirm$
    WITH due AS (
        SELECT j.id
        FROM tron_sweep_jobs j
        WHERE j.status = 'SUBMITTED'
          AND (
              j.last_checked_at IS NULL
              OR j.last_checked_at < NOW() - INTERVAL '60 seconds'
          )
        ORDER BY j.last_checked_at NULLS FIRST, j.submitted_at
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 20), 1), 100)
    )
    UPDATE tron_sweep_jobs j
    SET last_checked_at = NOW(), updated_at = NOW()
    FROM due
    WHERE j.id = due.id
    RETURNING j.id, j.tenant_id, j.txid
$confirm$;

CREATE SEQUENCE tron_deposit_index_seq
    AS BIGINT MINVALUE 0 START WITH 0 INCREMENT BY 1 NO CYCLE;

-- Atomically claim due TRC20 addresses. The function returns only the fields
-- required by the chain watcher and never exposes user or wallet data.
CREATE OR REPLACE FUNCTION claim_pending_tron_deposits(p_limit INTEGER DEFAULT 20)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    order_no VARCHAR,
    deposit_address VARCHAR,
    amount_minor BIGINT,
    status VARCHAR
)
LANGUAGE SQL
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $tron$
    WITH due AS (
        SELECT o.id
        FROM wallet_topup_orders o
        WHERE o.provider = 'USDT'
          AND o.deposit_address IS NOT NULL
          AND o.status IN ('PENDING', 'EXPIRED')
          AND o.created_at > NOW() - INTERVAL '7 days'
          AND (
              o.last_scanned_at IS NULL
              OR o.last_scanned_at < NOW() - INTERVAL '60 seconds'
          )
        ORDER BY o.last_scanned_at NULLS FIRST, o.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 20), 1), 100)
    )
    UPDATE wallet_topup_orders o
    SET status = CASE
            WHEN o.status = 'PENDING' AND o.expires_at <= NOW()
            THEN 'EXPIRED'
            ELSE o.status
        END,
        last_scanned_at = NOW(),
        updated_at = NOW()
    FROM due
    WHERE o.id = due.id
    RETURNING o.id, o.tenant_id, o.order_no, o.deposit_address,
              o.amount_minor, o.status
$tron$;

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
GRANT SELECT, INSERT, UPDATE ON tron_sweep_jobs TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON payment_webhook_events TO tgs_runtime;
GRANT USAGE, SELECT ON SEQUENCE tron_deposit_index_seq TO tgs_runtime;
REVOKE ALL ON FUNCTION claim_pending_tron_deposits(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_pending_tron_deposits(INTEGER) TO tgs_runtime;
REVOKE ALL ON FUNCTION claim_pending_tron_sweeps(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_pending_tron_sweeps(INTEGER) TO tgs_runtime;
REVOKE ALL ON FUNCTION claim_submitted_tron_sweeps(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_submitted_tron_sweeps(INTEGER) TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON wallet_ledger_entries TO tgs_runtime;
REVOKE UPDATE, DELETE ON wallet_ledger_entries FROM tgs_runtime;

COMMIT;
