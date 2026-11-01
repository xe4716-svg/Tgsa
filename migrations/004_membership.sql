ALTER TABLE users
    ADD COLUMN IF NOT EXISTS platform_admin BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE membership_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(40) NOT NULL UNIQUE,
    name VARCHAR(80) NOT NULL,
    level INTEGER NOT NULL UNIQUE CHECK (level >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE plan_features (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES membership_plans(id) ON DELETE CASCADE,
    feature_key VARCHAR(80) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    limit_value INTEGER CHECK (limit_value IS NULL OR limit_value >= 0),
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_id, feature_key)
);
CREATE INDEX idx_plan_features_key ON plan_features(feature_key, plan_id);

CREATE TABLE tenant_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL REFERENCES membership_plans(id),
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCELED', 'EXPIRED')),
    starts_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    grace_until TIMESTAMPTZ,
    billing_source VARCHAR(20) NOT NULL DEFAULT 'MANUAL'
        CHECK (billing_source IN ('MANUAL', 'PAYMENT_PROVIDER', 'PROMOTION')),
    external_subscription_id VARCHAR(160),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_tenant_subscriptions_current
    ON tenant_subscriptions(tenant_id, starts_at DESC);
CREATE UNIQUE INDEX uq_tenant_current_subscription
    ON tenant_subscriptions(tenant_id)
    WHERE status IN ('TRIALING', 'ACTIVE', 'PAST_DUE');

ALTER TABLE tenant_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_subscriptions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_subscriptions
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON membership_plans, plan_features, tenant_subscriptions
    TO tgs_runtime;

INSERT INTO membership_plans (code, name, level, sort_order)
VALUES
    ('FREE', '免费版', 0, 0),
    ('BASIC', '基础版', 1, 1),
    ('PRO', '专业版', 2, 2),
    ('ENTERPRISE', '企业版', 3, 3)
ON CONFLICT (code) DO NOTHING;

-- FREE: discovery and account inspection only; no channel or sticker cloning.
INSERT INTO plan_features (plan_id, feature_key, enabled, limit_value)
SELECT p.id, f.feature_key, f.enabled, f.limit_value
FROM membership_plans p
CROSS JOIN (VALUES
    ('group_search', TRUE, NULL::INTEGER),
    ('global_search', FALSE, NULL::INTEGER),
    ('bot_search', TRUE, NULL::INTEGER),
    ('link_import', TRUE, NULL::INTEGER),
    ('account_asset_scan', TRUE, NULL::INTEGER),
    ('channel_clone', FALSE, NULL::INTEGER),
    ('sticker_clone', FALSE, NULL::INTEGER),
    ('realtime_sync', FALSE, NULL::INTEGER),
    ('max_telegram_accounts', TRUE, 1),
    ('max_active_clone_tasks', TRUE, 0)
) AS f(feature_key, enabled, limit_value)
WHERE p.code = 'FREE'
ON CONFLICT (plan_id, feature_key) DO NOTHING;

-- BASIC: channel cloning, but sticker cloning remains a higher-tier feature.
INSERT INTO plan_features (plan_id, feature_key, enabled, limit_value)
SELECT p.id, f.feature_key, f.enabled, f.limit_value
FROM membership_plans p
CROSS JOIN (VALUES
    ('group_search', TRUE, NULL::INTEGER),
    ('global_search', TRUE, NULL::INTEGER),
    ('bot_search', TRUE, NULL::INTEGER),
    ('link_import', TRUE, NULL::INTEGER),
    ('account_asset_scan', TRUE, NULL::INTEGER),
    ('channel_clone', TRUE, NULL::INTEGER),
    ('sticker_clone', FALSE, NULL::INTEGER),
    ('realtime_sync', TRUE, NULL::INTEGER),
    ('max_telegram_accounts', TRUE, 2),
    ('max_active_clone_tasks', TRUE, 2)
) AS f(feature_key, enabled, limit_value)
WHERE p.code = 'BASIC'
ON CONFLICT (plan_id, feature_key) DO NOTHING;

-- PRO: channel and sticker cloning, with higher account/task limits.
INSERT INTO plan_features (plan_id, feature_key, enabled, limit_value)
SELECT p.id, f.feature_key, f.enabled, f.limit_value
FROM membership_plans p
CROSS JOIN (VALUES
    ('group_search', TRUE, NULL::INTEGER),
    ('global_search', TRUE, NULL::INTEGER),
    ('bot_search', TRUE, NULL::INTEGER),
    ('link_import', TRUE, NULL::INTEGER),
    ('account_asset_scan', TRUE, NULL::INTEGER),
    ('channel_clone', TRUE, NULL::INTEGER),
    ('sticker_clone', TRUE, NULL::INTEGER),
    ('realtime_sync', TRUE, NULL::INTEGER),
    ('max_telegram_accounts', TRUE, 10),
    ('max_active_clone_tasks', TRUE, 10)
) AS f(feature_key, enabled, limit_value)
WHERE p.code = 'PRO'
ON CONFLICT (plan_id, feature_key) DO NOTHING;

-- ENTERPRISE: unlimited limits (NULL) and all listed features enabled.
INSERT INTO plan_features (plan_id, feature_key, enabled, limit_value)
SELECT p.id, f.feature_key, f.enabled, f.limit_value
FROM membership_plans p
CROSS JOIN (VALUES
    ('group_search', TRUE, NULL::INTEGER),
    ('global_search', TRUE, NULL::INTEGER),
    ('bot_search', TRUE, NULL::INTEGER),
    ('link_import', TRUE, NULL::INTEGER),
    ('account_asset_scan', TRUE, NULL::INTEGER),
    ('channel_clone', TRUE, NULL::INTEGER),
    ('sticker_clone', TRUE, NULL::INTEGER),
    ('realtime_sync', TRUE, NULL::INTEGER),
    ('max_telegram_accounts', TRUE, NULL::INTEGER),
    ('max_active_clone_tasks', TRUE, NULL::INTEGER)
) AS f(feature_key, enabled, limit_value)
WHERE p.code = 'ENTERPRISE'
ON CONFLICT (plan_id, feature_key) DO NOTHING;
