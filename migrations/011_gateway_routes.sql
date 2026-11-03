-- Desired connection routing only. Business work stays in existing outboxes.
CREATE TABLE account_gateway_routes (
    tenant_id UUID NOT NULL,
    account_id UUID PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
    last_reason VARCHAR(40),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES telegram_accounts(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_gateway_routes_enabled ON account_gateway_routes(tenant_id, account_id) WHERE enabled;
ALTER TABLE account_gateway_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_gateway_routes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON account_gateway_routes
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON account_gateway_routes TO tgs_runtime;
