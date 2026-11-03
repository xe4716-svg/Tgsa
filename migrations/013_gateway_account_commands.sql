BEGIN;

-- Durable, narrowly typed command inbox for account-broadcast operations.
-- No application role is granted access here. A later provisioning change must
-- grant only the dedicated Gateway/submitter roles after their trust boundary
-- and RLS behavior have dedicated PostgreSQL integration tests.
CREATE TABLE public.gateway_account_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    account_id UUID NOT NULL,
    route_generation BIGINT NOT NULL CHECK (route_generation > 0),
    operation_key UUID NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('preflight_send_text')),
    destination_peer_id BIGINT NOT NULL,
    payload JSONB NOT NULL CHECK (
        jsonb_typeof(payload) = 'object'
        AND payload ? 'text'
        AND jsonb_typeof(payload->'text') = 'string'
        AND length(payload->>'text') BETWEEN 1 AND 4096
        AND (payload - 'text') = '{}'::jsonb
    ),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'claimed', 'succeeded', 'rejected', 'flood_wait',
        'unknown', 'cancelled'
    )),
    claim_worker_id TEXT,
    claim_fencing_token BIGINT CHECK (claim_fencing_token IS NULL OR claim_fencing_token > 0),
    claim_until TIMESTAMPTZ,
    dispatch_started_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    result JSONB CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    error_code TEXT CHECK (error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_gateway_account_command_operation
        UNIQUE (tenant_id, account_id, operation_key),
    CONSTRAINT fk_gateway_account_command_account
        FOREIGN KEY (tenant_id, account_id)
        REFERENCES public.telegram_accounts(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_gateway_command_claim_shape CHECK (
        (status = 'claimed' AND claim_worker_id IS NOT NULL
            AND claim_fencing_token IS NOT NULL AND claim_until IS NOT NULL)
        OR status <> 'claimed'
    ),
    CONSTRAINT ck_gateway_command_result_shape CHECK (
        (status IN ('succeeded', 'rejected', 'flood_wait', 'unknown', 'cancelled'))
        OR (result IS NULL AND error_code IS NULL AND completed_at IS NULL)
    )
);

CREATE INDEX idx_gateway_commands_ready
    ON public.gateway_account_commands(tenant_id, account_id, available_at, created_at)
    WHERE status = 'queued';
CREATE INDEX idx_gateway_commands_expired_claim
    ON public.gateway_account_commands(claim_until)
    WHERE status = 'claimed';
CREATE INDEX idx_gateway_commands_unknown
    ON public.gateway_account_commands(tenant_id, account_id, updated_at)
    WHERE status = 'unknown';

ALTER TABLE public.gateway_account_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gateway_account_commands FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON public.gateway_account_commands
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Sanitized append-only state transition log. No message body or Telegram secrets.
CREATE TABLE public.gateway_account_command_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id UUID NOT NULL,
    command_id UUID NOT NULL,
    account_id UUID NOT NULL,
    transition TEXT NOT NULL CHECK (transition IN (
        'submitted', 'claimed', 'dispatch_started', 'succeeded',
        'rejected', 'flood_wait', 'unknown', 'cancelled', 'reconciled'
    )),
    actor_class TEXT NOT NULL CHECK (actor_class IN ('business_worker', 'gateway', 'operator')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES public.telegram_accounts(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (command_id) REFERENCES public.gateway_account_commands(id) ON DELETE CASCADE
);
CREATE INDEX idx_gateway_command_events_command
    ON public.gateway_account_command_events(tenant_id, command_id, id);
ALTER TABLE public.gateway_account_command_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gateway_account_command_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON public.gateway_account_command_events
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Explicitly fail closed: no PUBLIC/runtime access until a separate least-privilege
-- role provisioning change grants column/function-level access and passes RLS tests.
REVOKE ALL ON public.gateway_account_commands FROM PUBLIC, tgs_runtime;
REVOKE ALL ON public.gateway_account_command_events FROM PUBLIC, tgs_runtime;

COMMIT;
