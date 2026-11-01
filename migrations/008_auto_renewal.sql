-- Auto-renewal intent, in-app notifications and email outbox.
-- Apply after 007_payment_orchestration.sql.

BEGIN;

ALTER TABLE tenant_subscriptions
    ADD CONSTRAINT uq_tenant_subscription_identity UNIQUE (tenant_id, id);

CREATE TABLE billing_automation_settings (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    renewal_advance_days INTEGER NOT NULL DEFAULT 3
        CHECK (renewal_advance_days IN (1, 3, 7)),
    reminder_days JSONB NOT NULL DEFAULT '[7, 3, 1, 0]'::jsonb
        CHECK (jsonb_typeof(reminder_days) = 'array'
               AND jsonb_array_length(reminder_days) <= 12),
    retry_interval_minutes INTEGER NOT NULL DEFAULT 60
        CHECK (retry_interval_minutes BETWEEN 5 AND 1440),
    max_technical_retries INTEGER NOT NULL DEFAULT 3
        CHECK (max_technical_retries BETWEEN 1 AND 3),
    notification_timezone_mode VARCHAR(12) NOT NULL DEFAULT 'PLATFORM'
        CHECK (notification_timezone_mode IN ('TENANT', 'PLATFORM')),
    platform_timezone VARCHAR(80) NOT NULL DEFAULT 'UTC',
    price_change_notice_hours INTEGER NOT NULL DEFAULT 24
        CHECK (price_change_notice_hours BETWEEN 1 AND 168),
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO billing_automation_settings (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE tenant_billing_preferences (
    tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    timezone VARCHAR(80) NOT NULL DEFAULT 'UTC',
    timezone_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    email_notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    telegram_notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE tenant_subscriptions
    ADD COLUMN expiry_notified_at TIMESTAMPTZ;

CREATE TABLE tenant_auto_renew_settings (
    tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    subscription_id UUID NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    currency VARCHAR(8) NOT NULL CHECK (currency IN ('CNY', 'USD', 'USDT')),
    duration_days INTEGER NOT NULL CHECK (duration_days BETWEEN 1 AND 3650),
    timezone VARCHAR(80) NOT NULL DEFAULT 'UTC',
    enabled_at TIMESTAMPTZ,
    last_price_minor BIGINT CHECK (last_price_minor IS NULL OR last_price_minor >= 0),
    last_price_version INTEGER CHECK (last_price_version IS NULL OR last_price_version > 0),
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, subscription_id)
        REFERENCES tenant_subscriptions(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_auto_renew_enabled
    ON tenant_auto_renew_settings(enabled, subscription_id)
    WHERE enabled = TRUE;

CREATE TABLE subscription_renewal_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    subscription_id UUID NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    renewal_cycle_key VARCHAR(120) NOT NULL,
    plan_id UUID NOT NULL REFERENCES membership_plans(id),
    plan_code_snapshot VARCHAR(40) NOT NULL,
    duration_days INTEGER NOT NULL CHECK (duration_days BETWEEN 1 AND 3650),
    currency VARCHAR(8) NOT NULL CHECK (currency IN ('CNY', 'USD', 'USDT')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor >= 0),
    price_version INTEGER NOT NULL CHECK (price_version > 0),
    request_fingerprint CHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN (
            'PENDING', 'PROCESSING', 'WAITING_FOR_BALANCE', 'RETRY',
            'PAID', 'CANCELED', 'FAILED', 'EXPIRED'
        )),
    idempotency_key VARCHAR(120) NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 3),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error_code VARCHAR(80),
    price_notice_sent_at TIMESTAMPTZ,
    paid_order_id UUID,
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, subscription_id, renewal_cycle_key),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, subscription_id)
        REFERENCES tenant_subscriptions(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, paid_order_id)
        REFERENCES billing_orders(tenant_id, id)
);
CREATE INDEX idx_renewal_intents_due
    ON subscription_renewal_intents(status, next_attempt_at, created_at)
    WHERE status IN ('PENDING', 'WAITING_FOR_BALANCE', 'RETRY', 'PROCESSING');

CREATE TABLE tenant_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dedupe_key VARCHAR(160) NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    title VARCHAR(160) NOT NULL,
    body TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, dedupe_key)
);
CREATE INDEX idx_tenant_notifications_user_time
    ON tenant_notifications(tenant_id, user_id, created_at DESC);

CREATE TABLE billing_email_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dedupe_key VARCHAR(160) NOT NULL,
    subject VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error_code VARCHAR(80),
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, dedupe_key)
);
CREATE INDEX idx_billing_email_outbox_due
    ON billing_email_outbox(status, next_attempt_at, created_at)
    WHERE status IN ('PENDING', 'PROCESSING');

ALTER TABLE tenant_billing_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_billing_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_billing_preferences
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE tenant_auto_renew_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_auto_renew_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_auto_renew_settings
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE subscription_renewal_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscription_renewal_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON subscription_renewal_intents
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE tenant_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_notifications
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE billing_email_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_email_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON billing_email_outbox
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE OR REPLACE FUNCTION create_due_auto_renew_intents(p_limit INTEGER DEFAULT 100)
RETURNS INTEGER
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $create$
DECLARE
    inserted_count INTEGER := 0;
BEGIN
    WITH candidates AS (
        SELECT
            ar.tenant_id,
            ar.subscription_id,
            ar.user_id,
            s.plan_id,
            p.code AS plan_code,
            ar.duration_days,
            ar.currency,
            pr.amount_minor,
            pr.version,
            s.expires_at,
            s.id::text || ':' ||
                to_char(s.expires_at AT TIME ZONE 'UTC', 'YYYYMMDDHH24MISSUS') AS cycle_key
        FROM tenant_auto_renew_settings ar
        JOIN tenant_subscriptions s
          ON s.tenant_id = ar.tenant_id AND s.id = ar.subscription_id
        JOIN membership_plans p ON p.id = s.plan_id AND p.is_active = TRUE
        JOIN membership_plan_prices pr
          ON pr.plan_id = s.plan_id
         AND pr.currency = ar.currency
         AND pr.duration_days = ar.duration_days
         AND pr.is_current = TRUE
        CROSS JOIN billing_automation_settings cfg
        WHERE ar.enabled = TRUE
          AND s.status IN ('ACTIVE', 'TRIALING')
          AND s.expires_at IS NOT NULL
          AND s.expires_at > NOW()
          AND s.expires_at <= NOW() + make_interval(days => cfg.renewal_advance_days)
        ORDER BY s.expires_at
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 500)
    )
    INSERT INTO subscription_renewal_intents (
        tenant_id, subscription_id, user_id, renewal_cycle_key,
        plan_id, plan_code_snapshot, duration_days, currency,
        amount_minor, price_version, request_fingerprint, status,
        idempotency_key, max_attempts
    )
    SELECT
        c.tenant_id, c.subscription_id, c.user_id, c.cycle_key,
        c.plan_id, c.plan_code, c.duration_days, c.currency,
        c.amount_minor, c.version,
        encode(digest(
            c.plan_code || '|' || c.duration_days::text || '|' ||
            c.currency || '|' || c.amount_minor::text || '|' || c.version::text,
            'sha256'
        ), 'hex'),
        'PENDING',
        'auto-renew:' || c.cycle_key,
        cfg.max_technical_retries
    FROM candidates c
    CROSS JOIN billing_automation_settings cfg
    ON CONFLICT (tenant_id, subscription_id, renewal_cycle_key)
    DO UPDATE SET
        plan_id = EXCLUDED.plan_id,
        plan_code_snapshot = EXCLUDED.plan_code_snapshot,
        duration_days = EXCLUDED.duration_days,
        currency = EXCLUDED.currency,
        amount_minor = EXCLUDED.amount_minor,
        price_version = EXCLUDED.price_version,
        request_fingerprint = EXCLUDED.request_fingerprint,
        status = 'PENDING',
        idempotency_key = EXCLUDED.idempotency_key,
        attempt_count = 0,
        max_attempts = EXCLUDED.max_attempts,
        next_attempt_at = NOW(),
        last_error_code = NULL,
        price_notice_sent_at = NULL,
        updated_at = NOW()
    WHERE subscription_renewal_intents.status = 'CANCELED'
      AND subscription_renewal_intents.paid_order_id IS NULL;

    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    RETURN inserted_count;
END
$create$;

CREATE OR REPLACE FUNCTION claim_due_auto_renew_intents(p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    subscription_id UUID,
    user_id UUID,
    plan_id UUID,
    plan_code_snapshot VARCHAR,
    renewal_cycle_key VARCHAR,
    duration_days INTEGER,
    currency VARCHAR,
    amount_minor BIGINT,
    price_version INTEGER,
    request_fingerprint CHAR(64),
    attempt_count INTEGER,
    max_attempts INTEGER,
    status VARCHAR
)
LANGUAGE SQL
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $claim$
    WITH due AS (
        SELECT i.id
        FROM subscription_renewal_intents i
        WHERE (
            i.status IN ('PENDING', 'WAITING_FOR_BALANCE', 'RETRY')
            AND i.next_attempt_at <= NOW()
        ) OR (
            i.status = 'PROCESSING'
            AND i.updated_at < NOW() - INTERVAL '5 minutes'
        )
        ORDER BY i.next_attempt_at, i.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
    )
    UPDATE subscription_renewal_intents i
    SET status = 'PROCESSING', updated_at = NOW()
    FROM due
    WHERE i.id = due.id
    RETURNING i.id, i.tenant_id, i.subscription_id, i.user_id,
              i.plan_id, i.plan_code_snapshot, i.renewal_cycle_key,
              i.duration_days, i.currency, i.amount_minor, i.price_version,
              i.request_fingerprint, i.attempt_count, i.max_attempts, i.status
$claim$;

CREATE OR REPLACE FUNCTION claim_due_billing_emails(p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    user_id UUID,
    email VARCHAR,
    subject VARCHAR,
    body TEXT,
    attempt_count INTEGER
)
LANGUAGE SQL
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $email$
    WITH exhausted AS (
        UPDATE billing_email_outbox o
        SET status = 'FAILED',
            last_error_code = 'MAX_ATTEMPTS_EXCEEDED',
            updated_at = NOW()
        WHERE o.status = 'PROCESSING'
          AND o.updated_at < NOW() - INTERVAL '5 minutes'
          AND o.attempt_count >= o.max_attempts
        RETURNING o.id
    ),
    due AS (
        SELECT o.id
        FROM billing_email_outbox o
        WHERE o.attempt_count < o.max_attempts
          AND ((
            o.status = 'PENDING' AND o.next_attempt_at <= NOW()
          ) OR (
            o.status = 'PROCESSING'
            AND o.updated_at < NOW() - INTERVAL '5 minutes'
          ))
        ORDER BY o.next_attempt_at, o.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
    )
    UPDATE billing_email_outbox o
    SET status = 'PROCESSING',
        attempt_count = o.attempt_count + 1,
        updated_at = NOW()
    FROM due, users u
    WHERE o.id = due.id
      AND u.id = o.user_id
    RETURNING o.id, o.tenant_id, o.user_id,
              CASE WHEN u.email_verified_at IS NOT NULL THEN u.email ELSE NULL END,
              o.subject, o.body, o.attempt_count
$email$;

CREATE OR REPLACE FUNCTION list_billing_reminder_recipients(p_limit INTEGER DEFAULT 1000)
RETURNS TABLE (
    tenant_id UUID,
    subscription_id UUID,
    plan_code VARCHAR,
    plan_name VARCHAR,
    subscription_status VARCHAR,
    expires_at TIMESTAMPTZ,
    timezone VARCHAR,
    user_id UUID,
    email VARCHAR,
    email_notifications_enabled BOOLEAN
)
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $reminders$
    SELECT
        s.tenant_id, s.id, p.code, p.name, s.status, s.expires_at,
        CASE
            WHEN cfg.notification_timezone_mode = 'PLATFORM'
                THEN cfg.platform_timezone
            ELSE COALESCE(pref.timezone, ar.timezone, 'UTC')
        END,
        u.id,
        CASE WHEN u.email_verified_at IS NOT NULL THEN u.email ELSE NULL END,
        COALESCE(pref.email_notifications_enabled, TRUE)
    FROM tenant_subscriptions s
    JOIN membership_plans p ON p.id = s.plan_id
    JOIN tenant_memberships tm
      ON tm.tenant_id = s.tenant_id AND tm.role IN ('OWNER', 'ADMIN')
    JOIN users u ON u.id = tm.user_id AND u.status = 'ACTIVE'
    LEFT JOIN tenant_billing_preferences pref ON pref.tenant_id = s.tenant_id
    LEFT JOIN tenant_auto_renew_settings ar
      ON ar.tenant_id = s.tenant_id AND ar.subscription_id = s.id
    CROSS JOIN billing_automation_settings cfg
    WHERE s.expires_at IS NOT NULL
      AND s.status IN ('ACTIVE', 'TRIALING', 'PAST_DUE', 'EXPIRED')
      AND (
          s.expires_at <= NOW() + INTERVAL '366 days'
          OR (s.status = 'EXPIRED' AND s.expiry_notified_at IS NULL)
      )
    ORDER BY s.expires_at
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 1000), 1), 5000)
$reminders$;

REVOKE ALL ON FUNCTION list_billing_reminder_recipients(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_billing_reminder_recipients(INTEGER) TO tgs_runtime;

REVOKE ALL ON FUNCTION create_due_auto_renew_intents(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION create_due_auto_renew_intents(INTEGER) TO tgs_runtime;
REVOKE ALL ON FUNCTION claim_due_auto_renew_intents(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_due_auto_renew_intents(INTEGER) TO tgs_runtime;
REVOKE ALL ON FUNCTION claim_due_billing_emails(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_due_billing_emails(INTEGER) TO tgs_runtime;

GRANT SELECT, INSERT, UPDATE ON billing_automation_settings TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_billing_preferences TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_auto_renew_settings TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON subscription_renewal_intents TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON tenant_notifications TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE ON billing_email_outbox TO tgs_runtime;

COMMIT;
