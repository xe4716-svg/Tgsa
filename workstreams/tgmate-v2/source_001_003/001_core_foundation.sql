-- TGMate V2.0 / Migration 001: secure shared foundation
-- PostgreSQL 15+
-- CLEAN BASELINE ONLY: do not apply on top of the old V1 draft.
--
-- Deployment requirements:
--   * Use a dedicated migration owner and a separate runtime role.
--   * Runtime role must be NOSUPERUSER, NOBYPASSRLS, and must not own tables.
--   * Derive tenant context server-side and SET LOCAL app.tenant_id per transaction.
--   * Revoke direct runtime access to global identity/admin/catalog tables.
--   * Keep Bot Tokens, MTProto session material and proxy credentials in KMS/Vault.
--   * This migration is a schema baseline, not a substitute for service-level
--     atomic quota, task lease, outbox and delivery reconciliation logic.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE SCHEMA IF NOT EXISTS tgmate;

CREATE OR REPLACE FUNCTION tgmate.current_tenant_id()
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION tgmate.touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION tgmate.reject_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_logs is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION tgmate.reject_immutable_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

-- Global identity / tenant registry. Restrict direct runtime grants.
CREATE TABLE tgmate.users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext,
  phone_e164 text,
  display_name text,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','suspended','deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (email IS NOT NULL OR phone_e164 IS NOT NULL)
);
CREATE UNIQUE INDEX users_email_uq ON tgmate.users(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX users_phone_uq ON tgmate.users(phone_e164) WHERE phone_e164 IS NOT NULL;

CREATE TABLE tgmate.tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  slug text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','suspended','closed')),
  timezone text NOT NULL DEFAULT 'UTC',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tgmate.tenant_members (
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  user_id uuid NOT NULL REFERENCES tgmate.users(id),
  status text NOT NULL DEFAULT 'invited'
    CHECK (status IN ('invited','active','suspended','removed')),
  joined_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,user_id)
);

CREATE TABLE tgmate.platform_admins (
  user_id uuid PRIMARY KEY REFERENCES tgmate.users(id),
  admin_level text NOT NULL
    CHECK (admin_level IN ('super_admin','operations','support','finance','security')),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','suspended')),
  granted_at timestamptz NOT NULL DEFAULT now(),
  granted_by uuid REFERENCES tgmate.users(id)
);

-- Tenant-owned RBAC. Composite FKs prevent cross-tenant role assignment.
CREATE TABLE tgmate.permissions (
  code text PRIMARY KEY,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tgmate.roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  code text NOT NULL,
  name text NOT NULL,
  is_system boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,code)
);

CREATE TABLE tgmate.role_permissions (
  tenant_id uuid NOT NULL,
  role_id uuid NOT NULL,
  permission_code text NOT NULL REFERENCES tgmate.permissions(code),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,role_id,permission_code),
  FOREIGN KEY (tenant_id,role_id)
    REFERENCES tgmate.roles(tenant_id,id) ON DELETE CASCADE
);

CREATE TABLE tgmate.tenant_member_roles (
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  role_id uuid NOT NULL,
  assigned_by uuid REFERENCES tgmate.users(id),
  assigned_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,user_id,role_id),
  FOREIGN KEY (tenant_id,user_id)
    REFERENCES tgmate.tenant_members(tenant_id,user_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id,role_id)
    REFERENCES tgmate.roles(tenant_id,id) ON DELETE CASCADE
);

-- Exactly five robot products. Account broadcast is a separate business channel.
CREATE TABLE tgmate.robot_product_types (
  code text PRIMARY KEY,
  name text NOT NULL,
  description text NOT NULL,
  is_enabled boolean NOT NULL DEFAULT true,
  sort_order smallint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO tgmate.robot_product_types(code,name,description,sort_order) VALUES
 ('broadcast','群发机器人','Bot API 群组/频道群发',10),
 ('moderation','群管理机器人','欢迎、验证、审核和群组管理',20),
 ('bidirectional','双向机器人','经双方同意的一对一消息中转',30),
 ('channel_clone','频道群组克隆机器人','经授权的历史克隆与实时同步',40),
 ('sticker_clone','贴纸克隆机器人','贴纸包、动态贴纸和自定义 Emoji 处理',50)
ON CONFLICT (code) DO UPDATE SET
  name=EXCLUDED.name, description=EXCLUDED.description, sort_order=EXCLUDED.sort_order;

CREATE TABLE tgmate.robot_instances (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  product_type_code text NOT NULL REFERENCES tgmate.robot_product_types(code),
  name text NOT NULL,
  status text NOT NULL DEFAULT 'not_configured'
    CHECK (status IN ('not_configured','ready','running','paused','degraded',
                      'error','authorization_expired','disabled','removed')),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES tgmate.users(id),
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id)
);
CREATE INDEX robot_instances_tenant_type_status_idx
  ON tgmate.robot_instances(tenant_id,product_type_code,status)
  WHERE deleted_at IS NULL;

-- Telegram identity and authorization are separate: one identity can have N sessions.
CREATE TABLE tgmate.telegram_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  telegram_user_id bigint,
  display_name text,
  username text,
  phone_masked text,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','restricted','suspended','removed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id)
);
CREATE UNIQUE INDEX telegram_identity_user_uq
  ON tgmate.telegram_identities(tenant_id,telegram_user_id)
  WHERE telegram_user_id IS NOT NULL;

CREATE TABLE tgmate.telegram_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  label text,
  session_vault_ref text NOT NULL,
  session_fingerprint text NOT NULL,
  authorization_status text NOT NULL DEFAULT 'pending_check'
    CHECK (authorization_status IN ('pending_check','authorized','expired',
                                    'revoked','invalid','suspended')),
  device_label text,
  security_observation_until timestamptz,
  last_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,session_fingerprint),
  FOREIGN KEY (tenant_id,telegram_identity_id)
    REFERENCES tgmate.telegram_identities(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX telegram_sessions_identity_status_idx
  ON tgmate.telegram_sessions(tenant_id,telegram_identity_id,authorization_status);

CREATE TABLE tgmate.telegram_bots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  display_name text,
  username text,
  telegram_bot_id bigint,
  token_vault_ref text NOT NULL,
  token_fingerprint text NOT NULL,
  status text NOT NULL DEFAULT 'pending_check'
    CHECK (status IN ('pending_check','active','invalid','revoked','suspended')),
  last_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,token_fingerprint)
);

-- Proxy configuration and stable egress assignment are separate concerns.
CREATE TABLE tgmate.egress_nodes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_code text NOT NULL UNIQUE,
  region_code text NOT NULL,
  public_ip inet,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','degraded','disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tgmate.proxies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  name text NOT NULL,
  proxy_type text NOT NULL
    CHECK (proxy_type IN ('socks5','mtproto','http_connect')),
  host text NOT NULL,
  port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
  credential_vault_ref text,
  sharing_mode text NOT NULL DEFAULT 'shared'
    CHECK (sharing_mode IN ('dedicated','shared')),
  status text NOT NULL DEFAULT 'untested'
    CHECK (status IN ('untested','healthy','degraded','failed')),
  last_tested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id)
);

CREATE TABLE tgmate.account_proxy_bindings (
  tenant_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  proxy_id uuid NOT NULL,
  is_primary boolean NOT NULL DEFAULT true,
  bound_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,telegram_identity_id,proxy_id),
  FOREIGN KEY (tenant_id,telegram_identity_id)
    REFERENCES tgmate.telegram_identities(tenant_id,id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id,proxy_id)
    REFERENCES tgmate.proxies(tenant_id,id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX account_one_primary_proxy_uq
  ON tgmate.account_proxy_bindings(tenant_id,telegram_identity_id)
  WHERE is_primary;

CREATE TABLE tgmate.account_egress_assignments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  egress_node_id uuid NOT NULL REFERENCES tgmate.egress_nodes(id),
  is_active boolean NOT NULL DEFAULT true,
  assigned_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  reason text,
  UNIQUE (tenant_id,id),
  FOREIGN KEY (tenant_id,telegram_identity_id)
    REFERENCES tgmate.telegram_identities(tenant_id,id) ON DELETE CASCADE,
  CHECK ((is_active AND ended_at IS NULL) OR NOT is_active)
);
CREATE UNIQUE INDEX one_active_egress_per_identity_uq
  ON tgmate.account_egress_assignments(tenant_id,telegram_identity_id)
  WHERE is_active;

CREATE TABLE tgmate.proxy_health_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  proxy_id uuid NOT NULL,
  result text NOT NULL
    CHECK (result IN ('success','timeout','auth_failed','protocol_mismatch',
                      'unreachable','error')),
  latency_ms integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
  exit_ip inet,
  region_code text,
  checked_at timestamptz NOT NULL DEFAULT now(),
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (tenant_id,proxy_id)
    REFERENCES tgmate.proxies(tenant_id,id) ON DELETE CASCADE
);

CREATE TABLE tgmate.network_route_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  event_type text NOT NULL,
  previous_egress_id uuid REFERENCES tgmate.egress_nodes(id),
  new_egress_id uuid REFERENCES tgmate.egress_nodes(id),
  reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id,telegram_identity_id)
    REFERENCES tgmate.telegram_identities(tenant_id,id) ON DELETE CASCADE
);

CREATE TABLE tgmate.executor_bindings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  robot_instance_id uuid NOT NULL,
  telegram_bot_id uuid,
  telegram_session_id uuid,
  purpose text NOT NULL
    CHECK (purpose IN ('send','source_read','target_write','event_receive',
                       'group_manage','sticker_manage')),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','paused','invalid')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  CHECK ((telegram_bot_id IS NOT NULL AND telegram_session_id IS NULL)
      OR (telegram_bot_id IS NULL AND telegram_session_id IS NOT NULL)),
  FOREIGN KEY (tenant_id,robot_instance_id)
    REFERENCES tgmate.robot_instances(tenant_id,id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id,telegram_bot_id)
    REFERENCES tgmate.telegram_bots(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,telegram_session_id)
    REFERENCES tgmate.telegram_sessions(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX executor_bindings_robot_purpose_idx
  ON tgmate.executor_bindings(tenant_id,robot_instance_id,purpose,status);

-- Plans are global catalogue data; subscriptions snapshot purchased entitlements.
CREATE TABLE tgmate.plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','active','retired')),
  billing_period text NOT NULL DEFAULT 'monthly'
    CHECK (billing_period IN ('monthly','yearly','one_time','custom')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tgmate.plan_entitlements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id uuid NOT NULL REFERENCES tgmate.plans(id) ON DELETE CASCADE,
  entitlement_code text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  limit_value bigint CHECK (limit_value IS NULL OR limit_value >= 0),
  limit_unit text,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (plan_id,entitlement_code)
);

CREATE TABLE tgmate.subscriptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  plan_id uuid NOT NULL REFERENCES tgmate.plans(id),
  status text NOT NULL
    CHECK (status IN ('pending','active','past_due','expired','cancelled','suspended')),
  starts_at timestamptz NOT NULL,
  ends_at timestamptz,
  auto_renew boolean NOT NULL DEFAULT false,
  price_amount numeric(20,8) NOT NULL DEFAULT 0 CHECK (price_amount >= 0),
  currency char(3) NOT NULL DEFAULT 'USD',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  CHECK (ends_at IS NULL OR ends_at > starts_at)
);
CREATE INDEX subscriptions_tenant_status_end_idx
  ON tgmate.subscriptions(tenant_id,status,ends_at);

CREATE TABLE tgmate.subscription_entitlements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  subscription_id uuid NOT NULL,
  entitlement_code text NOT NULL,
  enabled boolean NOT NULL,
  limit_value bigint CHECK (limit_value IS NULL OR limit_value >= 0),
  limit_unit text,
  config_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  valid_from timestamptz NOT NULL,
  valid_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  FOREIGN KEY (tenant_id,subscription_id)
    REFERENCES tgmate.subscriptions(tenant_id,id) ON DELETE RESTRICT,
  CHECK (valid_until IS NULL OR valid_until > valid_from)
);

-- Quota account counters are updated only by transactional quota services.
CREATE TABLE tgmate.quota_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  entitlement_code text NOT NULL,
  period_start timestamptz NOT NULL,
  period_end timestamptz NOT NULL,
  limit_value bigint CHECK (limit_value IS NULL OR limit_value >= 0),
  reserved_value bigint NOT NULL DEFAULT 0 CHECK (reserved_value >= 0),
  consumed_value bigint NOT NULL DEFAULT 0 CHECK (consumed_value >= 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,entitlement_code,period_start,period_end),
  CHECK (period_end > period_start),
  CHECK (limit_value IS NULL OR reserved_value + consumed_value <= limit_value)
);

-- Unified task registry. Account broadcast is a separate business channel,
-- not a sixth robot. Its account bindings are created in migration 002.
CREATE TABLE tgmate.task_registry (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  business_module text NOT NULL
    CHECK (business_module IN ('bot_broadcast','account_broadcast','moderation',
                               'bidirectional','channel_clone','sticker_clone')),
  robot_instance_id uuid,
  task_type text NOT NULL,
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','queued','running','pausing','paused','needs_attention',
                      'partially_completed','completed','failed','cancelled')),
  idempotency_key text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  progress jsonb NOT NULL DEFAULT '{}'::jsonb,
  scheduled_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,business_module,idempotency_key),
  FOREIGN KEY (tenant_id,robot_instance_id)
    REFERENCES tgmate.robot_instances(tenant_id,id) ON DELETE RESTRICT,
  CHECK ((business_module = 'account_broadcast' AND robot_instance_id IS NULL)
      OR (business_module <> 'account_broadcast' AND robot_instance_id IS NOT NULL))
);
CREATE INDEX task_dispatch_idx
  ON tgmate.task_registry(status,scheduled_at,created_at)
  WHERE status IN ('queued','needs_attention');
CREATE INDEX task_tenant_robot_history_idx
  ON tgmate.task_registry(tenant_id,robot_instance_id,created_at DESC);

CREATE OR REPLACE FUNCTION tgmate.validate_task_robot_type()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_type text;
DECLARE expected_type text;
BEGIN
  IF NEW.business_module = 'account_broadcast' THEN
    IF NEW.robot_instance_id IS NOT NULL THEN
      RAISE EXCEPTION 'account_broadcast must not bind to a robot instance';
    END IF;
    RETURN NEW;
  END IF;

  SELECT product_type_code INTO actual_type
  FROM tgmate.robot_instances
  WHERE tenant_id = NEW.tenant_id
    AND id = NEW.robot_instance_id
    AND deleted_at IS NULL;

  IF actual_type IS NULL THEN
    RAISE EXCEPTION 'robot instance not found or deleted';
  END IF;

  expected_type := CASE WHEN NEW.business_module = 'bot_broadcast'
                        THEN 'broadcast' ELSE NEW.business_module END;
  IF actual_type <> expected_type THEN
    RAISE EXCEPTION 'business_module does not match robot product type';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER task_robot_type_guard
  BEFORE INSERT OR UPDATE OF tenant_id,business_module,robot_instance_id
  ON tgmate.task_registry
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_task_robot_type();

-- Worker registry is platform infrastructure; heartbeat history is operational.
CREATE TABLE tgmate.worker_nodes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  worker_code text NOT NULL UNIQUE,
  worker_type text NOT NULL,
  status text NOT NULL DEFAULT 'starting'
    CHECK (status IN ('starting','healthy','degraded','offline','disabled')),
  last_heartbeat timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tgmate.worker_heartbeats (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  worker_id uuid NOT NULL REFERENCES tgmate.worker_nodes(id) ON DELETE CASCADE,
  status text NOT NULL,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX worker_heartbeats_worker_time_idx
  ON tgmate.worker_heartbeats(worker_id,recorded_at DESC);

CREATE TABLE tgmate.task_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  worker_id uuid REFERENCES tgmate.worker_nodes(id),
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  status text NOT NULL
    CHECK (status IN ('started','succeeded','failed','unknown','abandoned','manual_review')),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_code text,
  error_message text,
  result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,task_id,attempt_number),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT,
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE tgmate.task_leases (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  worker_id uuid NOT NULL REFERENCES tgmate.worker_nodes(id),
  lease_token uuid NOT NULL DEFAULT gen_random_uuid(),
  leased_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz NOT NULL,
  heartbeat_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,task_id),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE CASCADE,
  CHECK (lease_until > leased_at)
);
CREATE INDEX task_leases_expiry_idx ON tgmate.task_leases(lease_until);

CREATE TABLE tgmate.task_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  event_type text NOT NULL,
  event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX task_events_history_idx
  ON tgmate.task_events(tenant_id,task_id,created_at);
CREATE TRIGGER task_events_append_only
  BEFORE UPDATE OR DELETE ON tgmate.task_events
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_immutable_mutation();

CREATE TABLE tgmate.inbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  source_system text NOT NULL,
  external_event_id text NOT NULL,
  payload_hash text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'received'
    CHECK (status IN ('received','processing','processed','failed','ignored')),
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  UNIQUE (tenant_id,source_system,external_event_id)
);

CREATE TABLE tgmate.outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  idempotency_key text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','publishing','published','failed')),
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_by uuid REFERENCES tgmate.worker_nodes(id),
  locked_until timestamptz,
  published_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,idempotency_key),
  CHECK ((status = 'publishing' AND locked_until IS NOT NULL)
      OR status <> 'publishing')
);
CREATE INDEX outbox_dispatch_idx
  ON tgmate.outbox_events(available_at,created_at)
  WHERE status IN ('pending','failed','publishing');

CREATE TABLE tgmate.dead_letter_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  source_event_id uuid,
  event_type text NOT NULL,
  reason_code text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tgmate.task_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  destination_key text NOT NULL,
  source_item_key text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','succeeded','failed','unknown',
                      'manual_review','skipped')),
  remote_message_ref text,
  last_error_code text,
  last_attempt_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,task_id,destination_key,source_item_key),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE tgmate.delivery_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  delivery_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  status text NOT NULL CHECK (status IN ('started','succeeded','failed','unknown')),
  request_fingerprint text,
  remote_message_ref text,
  error_code text,
  error_detail text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id,delivery_id,attempt_number),
  FOREIGN KEY (tenant_id,delivery_id)
    REFERENCES tgmate.task_deliveries(tenant_id,id) ON DELETE RESTRICT,
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE tgmate.quota_reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  quota_account_id uuid NOT NULL,
  task_id uuid,
  entitlement_code text NOT NULL,
  amount bigint NOT NULL CHECK (amount > 0),
  status text NOT NULL DEFAULT 'reserved'
    CHECK (status IN ('reserved','committed','released','adjusted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  FOREIGN KEY (tenant_id,quota_account_id)
    REFERENCES tgmate.quota_accounts(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX quota_reservations_status_idx
  ON tgmate.quota_reservations(tenant_id,status,created_at);

CREATE TABLE tgmate.quota_ledger (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  quota_account_id uuid NOT NULL,
  reservation_id uuid,
  idempotency_key text NOT NULL,
  event_type text NOT NULL
    CHECK (event_type IN ('reserve','commit','release','admin_adjustment','expiry','refund')),
  reserved_delta bigint NOT NULL DEFAULT 0,
  consumed_delta bigint NOT NULL DEFAULT 0,
  actor_user_id uuid REFERENCES tgmate.users(id),
  reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id,quota_account_id)
    REFERENCES tgmate.quota_accounts(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,reservation_id)
    REFERENCES tgmate.quota_reservations(tenant_id,id) ON DELETE RESTRICT,
  UNIQUE (tenant_id,idempotency_key),
  CHECK (reserved_delta <> 0 OR consumed_delta <> 0)
);
CREATE INDEX quota_ledger_account_time_idx
  ON tgmate.quota_ledger(tenant_id,quota_account_id,created_at DESC);
CREATE TRIGGER quota_ledger_append_only
  BEFORE UPDATE OR DELETE ON tgmate.quota_ledger
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_immutable_mutation();

CREATE TABLE tgmate.audit_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  actor_user_id uuid REFERENCES tgmate.users(id),
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id uuid,
  request_id text,
  ip_hash text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_logs_tenant_time_idx
  ON tgmate.audit_logs(tenant_id,created_at DESC);
CREATE TRIGGER audit_logs_append_only
  BEFORE UPDATE OR DELETE ON tgmate.audit_logs
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_immutable_mutation();

CREATE TABLE tgmate.idempotency_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  scope text NOT NULL,
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  response_status integer,
  response_body jsonb,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,scope,idempotency_key)
);
CREATE INDEX idempotency_expiry_idx ON tgmate.idempotency_records(expires_at);

-- updated_at triggers
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','tenants','tenant_members','roles','robot_instances',
    'telegram_identities','telegram_sessions','telegram_bots','egress_nodes',
    'proxies','executor_bindings','plans','subscriptions','quota_accounts',
    'quota_reservations','task_registry','worker_nodes','task_deliveries'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON tgmate.%I '
      'FOR EACH ROW EXECUTE FUNCTION tgmate.touch_updated_at()',
      t || '_touch_updated_at', t
    );
  END LOOP;
END $$;

-- Tenant RLS. Global catalog and platform infrastructure tables are deliberately
-- not granted to tenant runtime roles; tenant data tables all carry tenant_id.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'tenant_members','roles','role_permissions','tenant_member_roles',
    'robot_instances','telegram_identities','telegram_sessions','telegram_bots',
    'proxies','account_proxy_bindings','account_egress_assignments',
    'proxy_health_checks','network_route_events','executor_bindings',
    'subscriptions','subscription_entitlements','quota_accounts',
    'task_registry','task_attempts','task_leases','task_events','inbox_events',
    'outbox_events','dead_letter_events','task_deliveries','delivery_attempts',
    'quota_reservations','quota_ledger','audit_logs','idempotency_records'
  ] LOOP
    EXECUTE format('ALTER TABLE tgmate.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE tgmate.%I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON tgmate.%I '
      'USING (tenant_id = tgmate.current_tenant_id()) '
      'WITH CHECK (tenant_id = tgmate.current_tenant_id())',t
    );
  END LOOP;
END $$;

COMMIT;

-- IMPORTANT: migration does not create runtime roles or grant privileges.
-- Apply a separately reviewed deployment-role migration before exposing the DB.
-- Quota reserve/commit/release, task lease acquisition/renewal, outbox claiming,
-- Inbox processing and unknown-delivery reconciliation are transactional
-- application services and must be covered by concurrency/failure tests.
