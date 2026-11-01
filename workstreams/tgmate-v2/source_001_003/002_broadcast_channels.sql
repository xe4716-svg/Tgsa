-- TGMate V2.0 / Migration 002: independent Bot API and MTProto broadcast channels
-- PostgreSQL 15+
-- Prerequisite: tgmate_v2_001_core_foundation.sql successfully applied.
-- Apply once. Do not apply to the old V1 schema.
--
-- Product boundary:
--   bot_broadcast     = one Bot API identity bound to a broadcast robot.
--   account_broadcast = independent MTProto user-account channel, NOT a sixth robot.
--
-- User-approved defaults:
--   independent accounts; shared template library; manual failure handling;
--   groups/supergroups only; immediate/scheduled/interval/recurring plans;
--   edits create a new version for future runs; pause/resume preserves checkpoints;
--   notifications default to in-app + Telegram + email.
--
-- Security: only send to groups where the selected executor is a member and
-- has the necessary permission. Respect Telegram rate limits/RetryAfter.
-- Never rotate identities/proxies to evade restrictions. Unknown send outcomes
-- must be reconciled or sent to manual review, never blindly resent.

BEGIN;

-- Composite keys used to ensure an executor/session belongs to the exact
-- tenant, robot, Bot or Telegram identity being referenced.
ALTER TABLE tgmate.telegram_sessions
  ADD CONSTRAINT telegram_sessions_tenant_session_identity_uq
  UNIQUE (tenant_id,id,telegram_identity_id);

ALTER TABLE tgmate.executor_bindings
  ADD CONSTRAINT executor_bindings_tenant_id_robot_bot_purpose_uq
  UNIQUE (tenant_id,id,robot_instance_id,telegram_bot_id,purpose);

-- Shared group directory. This broadcast product intentionally excludes channels.
CREATE TABLE tgmate.telegram_destinations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  telegram_peer_id bigint NOT NULL,
  destination_type text NOT NULL
    CHECK (destination_type IN ('group','supergroup')),
  title text NOT NULL,
  username text,
  discovery_source text NOT NULL DEFAULT 'manual'
    CHECK (discovery_source IN ('manual','bot','account','import')),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','inaccessible','left','removed')),
  last_seen_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,telegram_peer_id)
);
CREATE INDEX telegram_destinations_status_idx
  ON tgmate.telegram_destinations(tenant_id,status,destination_type);

CREATE TABLE tgmate.bot_destination_access (
  tenant_id uuid NOT NULL,
  telegram_bot_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  membership_status text NOT NULL DEFAULT 'unknown'
    CHECK (membership_status IN ('unknown','member','not_member','kicked')),
  can_send_messages boolean NOT NULL DEFAULT false,
  can_send_media boolean NOT NULL DEFAULT false,
  checked_at timestamptz,
  last_error_code text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,telegram_bot_id,destination_id),
  FOREIGN KEY (tenant_id,telegram_bot_id)
    REFERENCES tgmate.telegram_bots(tenant_id,id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id,destination_id)
    REFERENCES tgmate.telegram_destinations(tenant_id,id) ON DELETE CASCADE
);

CREATE TABLE tgmate.account_destination_access (
  tenant_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  membership_status text NOT NULL DEFAULT 'unknown'
    CHECK (membership_status IN ('unknown','member','not_member','banned','left')),
  can_send_messages boolean NOT NULL DEFAULT false,
  can_send_media boolean NOT NULL DEFAULT false,
  checked_at timestamptz,
  last_error_code text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,telegram_identity_id,destination_id),
  FOREIGN KEY (tenant_id,telegram_identity_id)
    REFERENCES tgmate.telegram_identities(tenant_id,id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id,destination_id)
    REFERENCES tgmate.telegram_destinations(tenant_id,id) ON DELETE CASCADE
);

-- Shared media library stores object-storage references, never raw media blobs.
CREATE TABLE tgmate.media_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  object_key text NOT NULL,
  sha256 text NOT NULL,
  mime_type text NOT NULL,
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  media_kind text NOT NULL
    CHECK (media_kind IN ('image','video','animation','audio','document','sticker','other')),
  scan_status text NOT NULL DEFAULT 'pending'
    CHECK (scan_status IN ('pending','clean','rejected','error')),
  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,object_key),
  UNIQUE (tenant_id,sha256)
);

-- Shared template library; published versions are immutable.
CREATE TABLE tgmate.message_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
  template_key text NOT NULL,
  name text NOT NULL,
  description text,
  supports_bot_api boolean NOT NULL DEFAULT true,
  supports_mtproto boolean NOT NULL DEFAULT true,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','archived')),
  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,template_key)
);

CREATE TABLE tgmate.message_template_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  template_id uuid NOT NULL,
  version_number integer NOT NULL CHECK (version_number > 0),
  content_type text NOT NULL
    CHECK (content_type IN ('text','photo','video','animation','document','audio','album','mixed')),
  content_payload jsonb NOT NULL,
  content_sha256 text NOT NULL,
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','published','retired')),
  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,template_id,version_number),
  FOREIGN KEY (tenant_id,template_id)
    REFERENCES tgmate.message_templates(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE tgmate.message_template_media (
  tenant_id uuid NOT NULL,
  template_version_id uuid NOT NULL,
  media_asset_id uuid NOT NULL,
  role text NOT NULL DEFAULT 'attachment',
  ordinal integer NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,template_version_id,media_asset_id,role,ordinal),
  FOREIGN KEY (tenant_id,template_version_id)
    REFERENCES tgmate.message_template_versions(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,media_asset_id)
    REFERENCES tgmate.media_assets(tenant_id,id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION tgmate.reject_broadcast_definition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is immutable; create a new version instead', TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE FUNCTION tgmate.guard_template_version_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'template versions cannot be deleted';
  END IF;

  IF ROW(NEW.tenant_id,NEW.template_id,NEW.version_number,NEW.content_type,
         NEW.content_payload,NEW.content_sha256,NEW.created_by,NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.template_id,OLD.version_number,OLD.content_type,
         OLD.content_payload,OLD.content_sha256,OLD.created_by,OLD.created_at) THEN
    RAISE EXCEPTION 'template content is immutable; create a new version';
  END IF;

  IF NOT (
    (OLD.status='draft' AND NEW.status IN ('draft','published','retired'))
    OR (OLD.status='published' AND NEW.status IN ('published','retired'))
    OR (OLD.status='retired' AND NEW.status='retired')
  ) THEN
    RAISE EXCEPTION 'invalid template version status transition';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER message_template_versions_immutable
  BEFORE UPDATE OR DELETE ON tgmate.message_template_versions
  FOR EACH ROW EXECUTE FUNCTION tgmate.guard_template_version_mutation();

CREATE TRIGGER message_template_media_immutable
  BEFORE UPDATE OR DELETE ON tgmate.message_template_media
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_broadcast_definition_mutation();

-- Every task edit creates a new immutable version. Runs remain pinned to a version.
CREATE TABLE tgmate.broadcast_task_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  version_number integer NOT NULL CHECK (version_number > 0),
  business_module text NOT NULL
    CHECK (business_module IN ('bot_broadcast','account_broadcast')),
  template_version_id uuid NOT NULL,
  schedule_mode text NOT NULL
    CHECK (schedule_mode IN ('immediate','scheduled','interval','recurring')),
  scheduled_at timestamptz,
  interval_seconds integer CHECK (interval_seconds IS NULL OR interval_seconds > 0),
  recurrence_config jsonb,
  timezone text NOT NULL DEFAULT 'UTC',
  target_scope text NOT NULL DEFAULT 'groups' CHECK (target_scope = 'groups'),
  failure_policy text NOT NULL DEFAULT 'manual' CHECK (failure_policy = 'manual'),
  notification_channels text[] NOT NULL
    DEFAULT ARRAY['in_app','telegram','email']::text[],
  config_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,task_id,version_number),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,template_version_id)
    REFERENCES tgmate.message_template_versions(tenant_id,id) ON DELETE RESTRICT,
  CHECK (notification_channels <@ ARRAY['in_app','telegram','email']::text[]),
  CHECK (
    (schedule_mode='immediate' AND scheduled_at IS NULL
      AND interval_seconds IS NULL AND recurrence_config IS NULL)
    OR
    (schedule_mode='scheduled' AND scheduled_at IS NOT NULL
      AND interval_seconds IS NULL AND recurrence_config IS NULL)
    OR
    (schedule_mode='interval' AND interval_seconds IS NOT NULL
      AND recurrence_config IS NULL)
    OR
    (schedule_mode='recurring' AND recurrence_config IS NOT NULL
      AND interval_seconds IS NULL)
  )
);
CREATE INDEX broadcast_task_versions_history_idx
  ON tgmate.broadcast_task_versions(tenant_id,task_id,version_number DESC);

CREATE OR REPLACE FUNCTION tgmate.validate_broadcast_task_version()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_module text;
BEGIN
  SELECT business_module INTO actual_module
  FROM tgmate.task_registry
  WHERE tenant_id=NEW.tenant_id AND id=NEW.task_id;

  IF actual_module IS DISTINCT FROM NEW.business_module THEN
    RAISE EXCEPTION 'broadcast version business_module does not match task';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER broadcast_task_version_module_guard
  BEFORE INSERT OR UPDATE ON tgmate.broadcast_task_versions
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_broadcast_task_version();

CREATE TABLE tgmate.broadcast_task_version_targets (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  version_number integer NOT NULL,
  destination_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,task_id,version_number,destination_id),
  FOREIGN KEY (tenant_id,task_id,version_number)
    REFERENCES tgmate.broadcast_task_versions(tenant_id,task_id,version_number)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,destination_id)
    REFERENCES tgmate.telegram_destinations(tenant_id,id) ON DELETE RESTRICT
);

CREATE TRIGGER broadcast_task_versions_immutable
  BEFORE UPDATE OR DELETE ON tgmate.broadcast_task_versions
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_broadcast_definition_mutation();

CREATE TRIGGER broadcast_task_version_targets_immutable
  BEFORE UPDATE OR DELETE ON tgmate.broadcast_task_version_targets
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_broadcast_definition_mutation();

-- A run is a snapshot of one task version; pause/resume changes checkpoints only.
CREATE TABLE tgmate.broadcast_task_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  version_number integer NOT NULL,
  business_module text NOT NULL
    CHECK (business_module IN ('bot_broadcast','account_broadcast')),
  run_number integer NOT NULL CHECK (run_number > 0),
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','preflight','running','pausing','paused','needs_attention',
                      'partially_completed','completed','failed','cancelled')),
  scheduled_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
  progress_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,task_id,run_number),
  UNIQUE (tenant_id,id,task_id),
  UNIQUE (tenant_id,id,task_id,version_number),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,task_id,version_number)
    REFERENCES tgmate.broadcast_task_versions(tenant_id,task_id,version_number)
    ON DELETE RESTRICT,
  CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX broadcast_task_runs_dispatch_idx
  ON tgmate.broadcast_task_runs(status,scheduled_at,created_at)
  WHERE status IN ('queued','preflight','needs_attention');

CREATE OR REPLACE FUNCTION tgmate.validate_broadcast_run_module()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE task_module text;
DECLARE version_module text;
BEGIN
  SELECT business_module INTO task_module
  FROM tgmate.task_registry
  WHERE tenant_id=NEW.tenant_id AND id=NEW.task_id;

  SELECT business_module INTO version_module
  FROM tgmate.broadcast_task_versions
  WHERE tenant_id=NEW.tenant_id AND task_id=NEW.task_id
    AND version_number=NEW.version_number;

  IF task_module IS DISTINCT FROM NEW.business_module
     OR version_module IS DISTINCT FROM NEW.business_module THEN
    RAISE EXCEPTION 'run business_module must match task and task version';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER broadcast_task_run_module_guard
  BEFORE INSERT OR UPDATE ON tgmate.broadcast_task_runs
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_broadcast_run_module();

CREATE TABLE tgmate.broadcast_run_targets (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  version_number integer NOT NULL,
  destination_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','ready','blocked','running','completed','failed','skipped')),
  checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_error_code text,
  checked_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,run_id,destination_id),
  UNIQUE (tenant_id,run_id,task_id,destination_id),
  FOREIGN KEY (tenant_id,run_id,task_id,version_number)
    REFERENCES tgmate.broadcast_task_runs(tenant_id,id,task_id,version_number)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,task_id,version_number,destination_id)
    REFERENCES tgmate.broadcast_task_version_targets(
      tenant_id,task_id,version_number,destination_id
    ) ON DELETE RESTRICT
);

-- Bot API path: one Bot executor per Bot broadcast task.
CREATE TABLE tgmate.bot_broadcast_tasks (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  robot_instance_id uuid NOT NULL,
  telegram_bot_id uuid NOT NULL,
  executor_binding_id uuid NOT NULL,
  executor_purpose text NOT NULL DEFAULT 'send' CHECK (executor_purpose='send'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,task_id),
  UNIQUE (tenant_id,task_id,telegram_bot_id,executor_binding_id),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,robot_instance_id)
    REFERENCES tgmate.robot_instances(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,telegram_bot_id)
    REFERENCES tgmate.telegram_bots(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,executor_binding_id,robot_instance_id,telegram_bot_id,executor_purpose)
    REFERENCES tgmate.executor_bindings(
      tenant_id,id,robot_instance_id,telegram_bot_id,purpose
    ) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION tgmate.validate_bot_broadcast_task()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_module text;
DECLARE actual_robot uuid;
DECLARE binding_status text;
BEGIN
  SELECT business_module,robot_instance_id
    INTO actual_module,actual_robot
  FROM tgmate.task_registry
  WHERE tenant_id=NEW.tenant_id AND id=NEW.task_id;

  IF actual_module IS DISTINCT FROM 'bot_broadcast'
     OR actual_robot IS DISTINCT FROM NEW.robot_instance_id THEN
    RAISE EXCEPTION 'Bot broadcast task must match bot_broadcast task and robot';
  END IF;

  SELECT status INTO binding_status
  FROM tgmate.executor_bindings
  WHERE tenant_id=NEW.tenant_id AND id=NEW.executor_binding_id
    AND robot_instance_id=NEW.robot_instance_id
    AND telegram_bot_id=NEW.telegram_bot_id AND purpose='send';

  IF binding_status IS DISTINCT FROM 'active' THEN
    RAISE EXCEPTION 'Bot send executor binding must be active';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER bot_broadcast_task_guard
  BEFORE INSERT OR UPDATE ON tgmate.bot_broadcast_tasks
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_bot_broadcast_task();

-- MTProto path: independent account assignments; no account rotation/pooling.
CREATE TABLE tgmate.account_broadcast_tasks (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  execution_mode text NOT NULL DEFAULT 'independent'
    CHECK (execution_mode='independent'),
  target_scope text NOT NULL DEFAULT 'groups' CHECK (target_scope='groups'),
  failure_policy text NOT NULL DEFAULT 'manual' CHECK (failure_policy='manual'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,task_id),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.task_registry(tenant_id,id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION tgmate.validate_account_broadcast_task()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_module text;
DECLARE actual_robot uuid;
BEGIN
  SELECT business_module,robot_instance_id
    INTO actual_module,actual_robot
  FROM tgmate.task_registry
  WHERE tenant_id=NEW.tenant_id AND id=NEW.task_id;

  IF actual_module IS DISTINCT FROM 'account_broadcast' OR actual_robot IS NOT NULL THEN
    RAISE EXCEPTION 'Account broadcast must be independent and not bind a robot';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER account_broadcast_task_guard
  BEFORE INSERT OR UPDATE ON tgmate.account_broadcast_tasks
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_account_broadcast_task();

CREATE TABLE tgmate.account_broadcast_task_accounts (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  telegram_session_id uuid NOT NULL,
  is_enabled boolean NOT NULL DEFAULT true,
  account_order integer NOT NULL DEFAULT 1 CHECK (account_order > 0),
  preflight_status text NOT NULL DEFAULT 'pending'
    CHECK (preflight_status IN ('pending','ready','blocked','needs_review')),
  last_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,task_id,telegram_identity_id),
  UNIQUE (tenant_id,task_id,account_order),
  UNIQUE (tenant_id,task_id,telegram_identity_id,telegram_session_id),
  FOREIGN KEY (tenant_id,task_id)
    REFERENCES tgmate.account_broadcast_tasks(tenant_id,task_id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,telegram_identity_id)
    REFERENCES tgmate.telegram_identities(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,telegram_session_id,telegram_identity_id)
    REFERENCES tgmate.telegram_sessions(tenant_id,id,telegram_identity_id)
    ON DELETE RESTRICT
);

CREATE INDEX account_broadcast_task_accounts_enabled_idx
  ON tgmate.account_broadcast_task_accounts(tenant_id,task_id,account_order)
  WHERE is_enabled;

-- Run-time snapshots of the executor identities selected when a run is created.
CREATE TABLE tgmate.bot_broadcast_run_executors (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_bot_id uuid NOT NULL,
  executor_binding_id uuid NOT NULL,
  preflight_status text NOT NULL DEFAULT 'pending'
    CHECK (preflight_status IN ('pending','ready','blocked','needs_review')),
  checked_at timestamptz,
  checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (tenant_id,run_id),
  UNIQUE (tenant_id,run_id,task_id,telegram_bot_id),
  FOREIGN KEY (tenant_id,run_id,task_id)
    REFERENCES tgmate.broadcast_task_runs(tenant_id,id,task_id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,task_id,telegram_bot_id,executor_binding_id)
    REFERENCES tgmate.bot_broadcast_tasks(
      tenant_id,task_id,telegram_bot_id,executor_binding_id
    ) ON DELETE RESTRICT
);

CREATE TABLE tgmate.account_broadcast_run_accounts (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  telegram_session_id uuid NOT NULL,
  preflight_status text NOT NULL DEFAULT 'pending'
    CHECK (preflight_status IN ('pending','ready','blocked','needs_review')),
  checked_at timestamptz,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','paused','needs_attention','completed','failed','skipped')),
  checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
  progress_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,run_id,telegram_identity_id),
  UNIQUE (tenant_id,run_id,task_id,telegram_identity_id),
  FOREIGN KEY (tenant_id,run_id,task_id)
    REFERENCES tgmate.broadcast_task_runs(tenant_id,id,task_id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,task_id,telegram_identity_id,telegram_session_id)
    REFERENCES tgmate.account_broadcast_task_accounts(
      tenant_id,task_id,telegram_identity_id,telegram_session_id
    ) ON DELETE RESTRICT
);

-- Per-executor access checks. A task target is not sendable until preflight passes.
CREATE TABLE tgmate.bot_broadcast_run_target_checks (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_bot_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  access_status text NOT NULL DEFAULT 'pending'
    CHECK (access_status IN ('pending','allowed','blocked','unknown')),
  can_send_messages boolean NOT NULL DEFAULT false,
  can_send_media boolean NOT NULL DEFAULT false,
  checked_at timestamptz,
  reason_code text,
  PRIMARY KEY (tenant_id,run_id,destination_id),
  UNIQUE (tenant_id,run_id,task_id,destination_id),
  FOREIGN KEY (tenant_id,run_id,task_id,telegram_bot_id)
    REFERENCES tgmate.bot_broadcast_run_executors(
      tenant_id,run_id,task_id,telegram_bot_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,run_id,task_id,destination_id)
    REFERENCES tgmate.broadcast_run_targets(
      tenant_id,run_id,task_id,destination_id
    ) ON DELETE RESTRICT
);

CREATE TABLE tgmate.account_broadcast_run_target_checks (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  access_status text NOT NULL DEFAULT 'pending'
    CHECK (access_status IN ('pending','allowed','blocked','unknown')),
  can_send_messages boolean NOT NULL DEFAULT false,
  can_send_media boolean NOT NULL DEFAULT false,
  checked_at timestamptz,
  reason_code text,
  PRIMARY KEY (tenant_id,run_id,telegram_identity_id,destination_id),
  UNIQUE (tenant_id,run_id,task_id,telegram_identity_id,destination_id),
  FOREIGN KEY (tenant_id,run_id,task_id,telegram_identity_id)
    REFERENCES tgmate.account_broadcast_run_accounts(
      tenant_id,run_id,task_id,telegram_identity_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,run_id,task_id,destination_id)
    REFERENCES tgmate.broadcast_run_targets(
      tenant_id,run_id,task_id,destination_id
    ) ON DELETE RESTRICT
);

-- Independent delivery ledgers and attempts for each protocol.
CREATE TABLE tgmate.bot_broadcast_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_bot_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  source_item_key text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','succeeded','failed','unknown','manual_review','skipped')),
  remote_message_ref text,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_error_code text,
  last_error_detail text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,run_id,telegram_bot_id,destination_id,source_item_key),
  FOREIGN KEY (tenant_id,run_id,task_id,telegram_bot_id)
    REFERENCES tgmate.bot_broadcast_run_executors(
      tenant_id,run_id,task_id,telegram_bot_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,run_id,task_id,destination_id)
    REFERENCES tgmate.bot_broadcast_run_target_checks(
      tenant_id,run_id,task_id,destination_id
    ) ON DELETE RESTRICT
);
CREATE OR REPLACE FUNCTION tgmate.require_bot_broadcast_preflight()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE access_ok boolean;
DECLARE message_ok boolean;
BEGIN
  SELECT (access_status='allowed'), can_send_messages
    INTO access_ok,message_ok
  FROM tgmate.bot_broadcast_run_target_checks
  WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
    AND task_id=NEW.task_id AND destination_id=NEW.destination_id;

  IF access_ok IS DISTINCT FROM true OR message_ok IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'Bot destination preflight has not allowed message sending';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER bot_broadcast_delivery_preflight_guard
  BEFORE INSERT ON tgmate.bot_broadcast_deliveries
  FOR EACH ROW EXECUTE FUNCTION tgmate.require_bot_broadcast_preflight();

CREATE INDEX bot_broadcast_deliveries_status_idx
  ON tgmate.bot_broadcast_deliveries(tenant_id,status,created_at);

CREATE TABLE tgmate.bot_broadcast_delivery_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  delivery_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  status text NOT NULL
    CHECK (status IN ('started','succeeded','failed','unknown','manual_review')),
  request_fingerprint text,
  remote_message_ref text,
  error_code text,
  error_detail text,
  retry_after_seconds integer CHECK (retry_after_seconds IS NULL OR retry_after_seconds >= 0),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id,delivery_id,attempt_number),
  FOREIGN KEY (tenant_id,delivery_id)
    REFERENCES tgmate.bot_broadcast_deliveries(tenant_id,id) ON DELETE RESTRICT,
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE tgmate.account_broadcast_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid NOT NULL,
  telegram_identity_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  source_item_key text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','succeeded','failed','unknown','manual_review','skipped')),
  remote_message_ref text,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_error_code text,
  last_error_detail text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,run_id,telegram_identity_id,destination_id,source_item_key),
  FOREIGN KEY (tenant_id,run_id,task_id,telegram_identity_id)
    REFERENCES tgmate.account_broadcast_run_accounts(
      tenant_id,run_id,task_id,telegram_identity_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,run_id,task_id,telegram_identity_id,destination_id)
    REFERENCES tgmate.account_broadcast_run_target_checks(
      tenant_id,run_id,task_id,telegram_identity_id,destination_id
    ) ON DELETE RESTRICT
);
CREATE OR REPLACE FUNCTION tgmate.require_account_broadcast_preflight()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE access_ok boolean;
DECLARE message_ok boolean;
BEGIN
  SELECT (access_status='allowed'), can_send_messages
    INTO access_ok,message_ok
  FROM tgmate.account_broadcast_run_target_checks
  WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
    AND task_id=NEW.task_id
    AND telegram_identity_id=NEW.telegram_identity_id
    AND destination_id=NEW.destination_id;

  IF access_ok IS DISTINCT FROM true OR message_ok IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'Account destination preflight has not allowed message sending';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER account_broadcast_delivery_preflight_guard
  BEFORE INSERT ON tgmate.account_broadcast_deliveries
  FOR EACH ROW EXECUTE FUNCTION tgmate.require_account_broadcast_preflight();

CREATE INDEX account_broadcast_deliveries_status_idx
  ON tgmate.account_broadcast_deliveries(tenant_id,status,created_at);

CREATE TABLE tgmate.account_broadcast_delivery_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  delivery_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  status text NOT NULL
    CHECK (status IN ('started','succeeded','failed','unknown','manual_review')),
  request_fingerprint text,
  remote_message_ref text,
  error_code text,
  error_detail text,
  retry_after_seconds integer CHECK (retry_after_seconds IS NULL OR retry_after_seconds >= 0),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id,delivery_id,attempt_number),
  FOREIGN KEY (tenant_id,delivery_id)
    REFERENCES tgmate.account_broadcast_deliveries(tenant_id,id) ON DELETE RESTRICT,
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);

-- updated_at triggers
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'telegram_destinations','message_templates','broadcast_task_runs',
    'broadcast_run_targets','account_broadcast_task_accounts',
    'account_broadcast_run_accounts','bot_broadcast_deliveries',
    'account_broadcast_deliveries','bot_destination_access',
    'account_destination_access'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON tgmate.%I '
      'FOR EACH ROW EXECUTE FUNCTION tgmate.touch_updated_at()',
      t || '_touch_updated_at',t
    );
  END LOOP;
END $$;

-- RLS for every tenant-owned table introduced by this migration.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'telegram_destinations','bot_destination_access','account_destination_access',
    'media_assets','message_templates','message_template_versions',
    'message_template_media','broadcast_task_versions','broadcast_task_version_targets',
    'broadcast_task_runs','broadcast_run_targets','bot_broadcast_tasks',
    'account_broadcast_tasks','account_broadcast_task_accounts',
    'bot_broadcast_run_executors','account_broadcast_run_accounts',
    'bot_broadcast_run_target_checks','account_broadcast_run_target_checks',
    'bot_broadcast_deliveries','bot_broadcast_delivery_attempts',
    'account_broadcast_deliveries','account_broadcast_delivery_attempts'
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

-- Application services still required:
-- 1. Create task + first version + version targets in one transaction.
-- 2. Create each run from an immutable version and snapshot selected executors.
-- 3. Re-check membership/permissions before every run and before sending after
--    a sufficiently long pause.
-- 4. Create delivery rows only for allowed executor/target pairs.
-- 5. Keep Bot API and MTProto Worker pools, credentials, queues and rate limits
--    separate. Account execution is independent; no automatic account rotation.
-- 6. Pause/resume must preserve per-account/per-target checkpoints.
-- 7. Unknown Telegram send outcomes go to reconciliation/manual review.
-- 8. Implement transactional entitlement reservation and quota settlement.
-- 9. Apply Telegram RetryAfter and respect all platform limits.
