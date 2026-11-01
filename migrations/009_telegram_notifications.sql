-- Telegram notification linking and reliable delivery outbox.
-- Apply after 008_auto_renewal.sql.

BEGIN;

CREATE TABLE tenant_telegram_notification_links (
    tenant_id UUID NOT NULL,
    user_id UUID NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    username VARCHAR(64),
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, user_id),
    UNIQUE (tenant_id, telegram_user_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES tenant_memberships(tenant_id, user_id) ON DELETE CASCADE
);
CREATE INDEX idx_telegram_notification_links_user
    ON tenant_telegram_notification_links(user_id, tenant_id);

CREATE TABLE tenant_telegram_link_tokens (
    token_hash CHAR(64) PRIMARY KEY,
    tenant_id UUID NOT NULL,
    user_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES tenant_memberships(tenant_id, user_id) ON DELETE CASCADE
);
CREATE INDEX idx_telegram_link_tokens_expiry
    ON tenant_telegram_link_tokens(expires_at)
    WHERE used_at IS NULL;

CREATE TABLE telegram_notification_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    user_id UUID NOT NULL,
    dedupe_key VARCHAR(160) NOT NULL,
    message_text TEXT NOT NULL CHECK (length(message_text) BETWEEN 1 AND 4096),
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error_code VARCHAR(80),
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, dedupe_key),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES tenant_memberships(tenant_id, user_id) ON DELETE CASCADE
);
CREATE INDEX idx_telegram_notification_outbox_due
    ON telegram_notification_outbox(status, next_attempt_at, created_at)
    WHERE status IN ('PENDING', 'PROCESSING');

ALTER TABLE tenant_telegram_notification_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_telegram_notification_links FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_telegram_notification_links
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE tenant_telegram_link_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_telegram_link_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tenant_telegram_link_tokens
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE telegram_notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_notification_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON telegram_notification_outbox
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE OR REPLACE FUNCTION consume_telegram_link_token(
    p_token_hash CHAR(64),
    p_telegram_user_id BIGINT,
    p_chat_id BIGINT,
    p_username TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $link$
DECLARE
    v_tenant_id UUID;
    v_user_id UUID;
    existing_user_id UUID;
BEGIN
    SELECT t.tenant_id, t.user_id
    INTO v_tenant_id, v_user_id
    FROM tenant_telegram_link_tokens t
    WHERE t.token_hash = p_token_hash
      AND t.used_at IS NULL
      AND t.expires_at > NOW()
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    SELECT l.user_id
    INTO existing_user_id
    FROM tenant_telegram_notification_links l
    WHERE l.tenant_id = v_tenant_id
      AND l.telegram_user_id = p_telegram_user_id;

    IF existing_user_id IS NOT NULL AND existing_user_id <> v_user_id THEN
        RETURN FALSE;
    END IF;

    INSERT INTO tenant_telegram_notification_links (
        tenant_id, user_id, telegram_user_id, chat_id, username, linked_at
    )
    VALUES (
        v_tenant_id, v_user_id, p_telegram_user_id, p_chat_id,
        NULLIF(left(p_username, 64), ''), NOW()
    )
    ON CONFLICT (tenant_id, user_id) DO UPDATE SET
        telegram_user_id = EXCLUDED.telegram_user_id,
        chat_id = EXCLUDED.chat_id,
        username = EXCLUDED.username,
        linked_at = NOW();

    UPDATE tenant_telegram_link_tokens
    SET used_at = NOW()
    WHERE token_hash = p_token_hash;

    INSERT INTO tenant_billing_preferences (
        tenant_id, telegram_notifications_enabled
    )
    VALUES (v_tenant_id, TRUE)
    ON CONFLICT (tenant_id) DO UPDATE SET
        telegram_notifications_enabled = TRUE,
        updated_at = NOW();

    RETURN TRUE;
EXCEPTION
    WHEN unique_violation THEN
        RETURN FALSE;
END
$link$;

CREATE OR REPLACE FUNCTION unlink_telegram_user(p_telegram_user_id BIGINT)
RETURNS INTEGER
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $unlink$
DECLARE
    item RECORD;
    removed_count INTEGER := 0;
BEGIN
    FOR item IN
        DELETE FROM tenant_telegram_notification_links
        WHERE telegram_user_id = p_telegram_user_id
        RETURNING tenant_id, user_id
    LOOP
        removed_count := removed_count + 1;

        UPDATE tenant_billing_preferences p
        SET telegram_notifications_enabled = EXISTS (
                SELECT 1
                FROM tenant_telegram_notification_links l
                WHERE l.tenant_id = item.tenant_id
            ),
            updated_at = NOW()
        WHERE p.tenant_id = item.tenant_id;

        UPDATE telegram_notification_outbox
        SET status = 'FAILED',
            last_error_code = 'TELEGRAM_LINK_REMOVED',
            updated_at = NOW()
        WHERE tenant_id = item.tenant_id
          AND user_id = item.user_id
          AND status IN ('PENDING', 'PROCESSING');
    END LOOP;

    RETURN removed_count;
END
$unlink$;

CREATE OR REPLACE FUNCTION claim_due_telegram_notifications(p_limit INTEGER DEFAULT 50)
RETURNS TABLE (
    id UUID,
    tenant_id UUID,
    user_id UUID,
    chat_id BIGINT,
    message_text TEXT,
    attempt_count INTEGER
)
LANGUAGE SQL
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $telegram$
    WITH exhausted AS (
        UPDATE telegram_notification_outbox o
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
        FROM telegram_notification_outbox o
        JOIN tenant_telegram_notification_links l
          ON l.tenant_id = o.tenant_id AND l.user_id = o.user_id
        JOIN tenant_billing_preferences p
          ON p.tenant_id = o.tenant_id
         AND p.telegram_notifications_enabled = TRUE
        WHERE o.attempt_count < o.max_attempts
          AND ((
              o.status = 'PENDING' AND o.next_attempt_at <= NOW()
          ) OR (
              o.status = 'PROCESSING'
              AND o.updated_at < NOW() - INTERVAL '5 minutes'
          ))
        ORDER BY o.next_attempt_at, o.created_at
        FOR UPDATE OF o SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
    )
    UPDATE telegram_notification_outbox o
    SET status = 'PROCESSING',
        attempt_count = o.attempt_count + 1,
        updated_at = NOW()
    FROM due, tenant_telegram_notification_links l
    WHERE o.id = due.id
      AND l.tenant_id = o.tenant_id
      AND l.user_id = o.user_id
    RETURNING o.id, o.tenant_id, o.user_id, l.chat_id,
              o.message_text, o.attempt_count
$telegram$;

REVOKE ALL ON FUNCTION consume_telegram_link_token(CHAR(64), BIGINT, BIGINT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION consume_telegram_link_token(CHAR(64), BIGINT, BIGINT, TEXT) TO tgs_runtime;
REVOKE ALL ON FUNCTION unlink_telegram_user(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION unlink_telegram_user(BIGINT) TO tgs_runtime;
REVOKE ALL ON FUNCTION claim_due_telegram_notifications(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_due_telegram_notifications(INTEGER) TO tgs_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON tenant_telegram_notification_links, tenant_telegram_link_tokens
    TO tgs_runtime;
GRANT SELECT, INSERT, UPDATE
    ON telegram_notification_outbox TO tgs_runtime;

COMMIT;
