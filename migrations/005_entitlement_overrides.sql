-- Per-tenant feature and quota exceptions; applied on top of the active plan.
-- Requires 004_membership.sql to have run first.
CREATE TABLE tenant_feature_overrides (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    feature_key VARCHAR(80) NOT NULL,
    enabled_override BOOLEAN,
    limit_delta INTEGER NOT NULL DEFAULT 0
        CHECK (limit_delta BETWEEN -1000000 AND 1000000),
    reason VARCHAR(500) NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, feature_key),
    CHECK (enabled_override IS NOT NULL OR limit_delta <> 0)
);

ALTER TABLE tenant_feature_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_feature_overrides FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_feature_overrides
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_feature_overrides TO tgs_runtime;
