-- TGMate V2.0 / Migration 003: Group Moderation + AI Moderation
-- PostgreSQL 15+
-- Prerequisites:
--   001 tgmate_v2_001_core_foundation.sql
--   002 tgmate_v2_002_broadcast_channels.sql
--
-- This migration adds group moderation configuration, welcome/verification,
-- rule-based moderation, violation windows, action/evidence/audit records,
-- appeals, AI assessment and human review.
--
-- Safe defaults:
--   cleanup_mode=auto_delete, but only when Telegram confirms delete permission;
--   without permission, log + notify and do not attempt the prohibited action.
--   violation window=24h; threshold=3 (tenant/group configurable).
--   auto-ban is OFF. Threshold action defaults to review.
--   join flow sends welcome first, then verification; timeout=60s, max attempts=3.
--   AI defaults to assist/review mode; AI alone cannot bypass permissions or
--   enable automatic bans.
--
-- Human verification: do not bypass CAPTCHA, slider, third-party challenges or
-- real-person checks. Escalate those to the group admin/user for manual handling.

BEGIN;

-- ---------------------------------------------------------------------------
-- Platform-wide defaults (global table; never grant to tenant runtime role).
-- Super-admin service reads this and copies defaults into a group config.
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_platform_defaults (
  singleton_id smallint PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
  cleanup_mode text NOT NULL DEFAULT 'auto_delete'
    CHECK (cleanup_mode IN ('auto_delete','log_only')),
  violation_window_seconds integer NOT NULL DEFAULT 86400
    CHECK (violation_window_seconds BETWEEN 60 AND 2592000),
  violation_threshold integer NOT NULL DEFAULT 3
    CHECK (violation_threshold BETWEEN 1 AND 100),
  auto_ban_enabled boolean NOT NULL DEFAULT false,
  verification_timeout_seconds integer NOT NULL DEFAULT 60
    CHECK (verification_timeout_seconds BETWEEN 15 AND 3600),
  verification_max_attempts smallint NOT NULL DEFAULT 3
    CHECK (verification_max_attempts BETWEEN 1 AND 10),
  ai_mode text NOT NULL DEFAULT 'assist'
    CHECK (ai_mode IN ('off','assist','enforce')),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_by uuid REFERENCES tgmate.users(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO tgmate.moderation_platform_defaults(singleton_id)
VALUES (1)
ON CONFLICT (singleton_id) DO NOTHING;

-- Extend the shared Bot/group access snapshot with moderation permissions.
ALTER TABLE tgmate.bot_destination_access
  ADD COLUMN can_delete_messages boolean NOT NULL DEFAULT false,
  ADD COLUMN can_restrict_members boolean NOT NULL DEFAULT false,
  ADD COLUMN can_ban_members boolean NOT NULL DEFAULT false;

-- ---------------------------------------------------------------------------
-- Group-level moderation configuration. One moderation controller per group.
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_group_configs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  destination_id uuid NOT NULL,
  robot_instance_id uuid NOT NULL,
  telegram_bot_id uuid NOT NULL,
  executor_binding_id uuid NOT NULL,
  executor_purpose text NOT NULL DEFAULT 'group_manage'
    CHECK (executor_purpose = 'group_manage'),

  enabled boolean NOT NULL DEFAULT true,
  cleanup_mode text NOT NULL DEFAULT 'auto_delete'
    CHECK (cleanup_mode IN ('auto_delete','log_only')),
  no_permission_policy text NOT NULL DEFAULT 'log_notify'
    CHECK (no_permission_policy = 'log_notify'),

  violation_window_seconds integer NOT NULL DEFAULT 86400
    CHECK (violation_window_seconds BETWEEN 60 AND 2592000),
  violation_threshold integer NOT NULL DEFAULT 3
    CHECK (violation_threshold BETWEEN 1 AND 100),
  threshold_action text NOT NULL DEFAULT 'review'
    CHECK (threshold_action IN ('review','warn','restrict','ban')),
  auto_ban_enabled boolean NOT NULL DEFAULT false,

  welcome_enabled boolean NOT NULL DEFAULT true,
  welcome_payload jsonb NOT NULL DEFAULT '{"text":"Welcome"}'::jsonb,
  join_flow_order text NOT NULL DEFAULT 'welcome_then_verify'
    CHECK (join_flow_order = 'welcome_then_verify'),
  temporary_restrict_on_join boolean NOT NULL DEFAULT true,
  verification_enabled boolean NOT NULL DEFAULT true,
  verification_timeout_seconds integer NOT NULL DEFAULT 60
    CHECK (verification_timeout_seconds BETWEEN 15 AND 3600),
  verification_max_attempts smallint NOT NULL DEFAULT 3
    CHECK (verification_max_attempts BETWEEN 1 AND 10),
  verification_success_payload jsonb,

  ai_mode text NOT NULL DEFAULT 'assist'
    CHECK (ai_mode IN ('off','assist','enforce')),
  ai_confidence_threshold numeric(5,4) NOT NULL DEFAULT 0.9500
    CHECK (ai_confidence_threshold BETWEEN 0 AND 1),
  human_review_required_for_ban boolean NOT NULL DEFAULT true,

  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,destination_id),

  FOREIGN KEY (tenant_id,destination_id)
    REFERENCES tgmate.telegram_destinations(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,robot_instance_id)
    REFERENCES tgmate.robot_instances(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,telegram_bot_id,destination_id)
    REFERENCES tgmate.bot_destination_access(tenant_id,telegram_bot_id,destination_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,executor_binding_id,robot_instance_id,telegram_bot_id,executor_purpose)
    REFERENCES tgmate.executor_bindings(
      tenant_id,id,robot_instance_id,telegram_bot_id,purpose
    ) ON DELETE RESTRICT,

  CHECK (threshold_action <> 'ban' OR auto_ban_enabled)
);

CREATE INDEX moderation_group_configs_enabled_idx
  ON tgmate.moderation_group_configs(tenant_id,enabled,updated_at DESC);

CREATE OR REPLACE FUNCTION tgmate.validate_moderation_robot_type()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_type text;
DECLARE binding_status text;
BEGIN
  SELECT product_type_code INTO actual_type
  FROM tgmate.robot_instances
  WHERE tenant_id=NEW.tenant_id AND id=NEW.robot_instance_id
    AND deleted_at IS NULL;

  IF actual_type IS DISTINCT FROM 'moderation' THEN
    RAISE EXCEPTION 'moderation group config requires a moderation robot instance';
  END IF;

  SELECT status INTO binding_status
  FROM tgmate.executor_bindings
  WHERE tenant_id=NEW.tenant_id AND id=NEW.executor_binding_id
    AND robot_instance_id=NEW.robot_instance_id
    AND telegram_bot_id=NEW.telegram_bot_id
    AND purpose='group_manage';

  IF binding_status IS DISTINCT FROM 'active' THEN
    RAISE EXCEPTION 'moderation executor binding must be active';
  END IF;

  IF NEW.threshold_action='ban' AND NOT NEW.auto_ban_enabled THEN
    RAISE EXCEPTION 'automatic ban requires explicit auto_ban_enabled';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER moderation_robot_type_guard
  BEFORE INSERT OR UPDATE ON tgmate.moderation_group_configs
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_moderation_robot_type();

CREATE OR REPLACE FUNCTION tgmate.validate_moderation_group_membership()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE membership text;
DECLARE must_check_membership boolean := false;
BEGIN
  IF TG_OP='INSERT' THEN
    must_check_membership := NEW.enabled;
  ELSIF NEW.enabled AND (
       NOT OLD.enabled
       OR NEW.telegram_bot_id IS DISTINCT FROM OLD.telegram_bot_id
       OR NEW.destination_id IS DISTINCT FROM OLD.destination_id
  ) THEN
    must_check_membership := true;
  END IF;

  IF must_check_membership THEN
    SELECT a.membership_status INTO membership
    FROM tgmate.bot_destination_access AS a
    WHERE a.tenant_id=NEW.tenant_id
      AND a.telegram_bot_id=NEW.telegram_bot_id
      AND a.destination_id=NEW.destination_id;

    IF membership IS DISTINCT FROM 'member' THEN
      RAISE EXCEPTION 'moderation Bot must be a confirmed member of the target group';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER moderation_group_membership_guard
  BEFORE INSERT OR UPDATE ON tgmate.moderation_group_configs
  FOR EACH ROW EXECUTE FUNCTION tgmate.validate_moderation_group_membership();

-- ---------------------------------------------------------------------------
-- Group member cache and exemptions. Admin/owner status is refreshed from
-- Telegram; explicit whitelist entries are managed by authorized admins.
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_group_members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  username text,
  display_name text,
  member_status text NOT NULL DEFAULT 'member'
    CHECK (member_status IN ('member','owner','administrator','restricted',
                             'left','kicked','unknown')),
  is_bot boolean NOT NULL DEFAULT false,
  joined_at timestamptz,
  last_seen_at timestamptz,
  last_role_check_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,group_config_id,telegram_user_id),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE CASCADE
);
CREATE INDEX moderation_group_members_role_idx
  ON tgmate.moderation_group_members(tenant_id,group_config_id,member_status);

CREATE TABLE tgmate.moderation_exemptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  exemption_type text NOT NULL DEFAULT 'whitelist'
    CHECK (exemption_type IN ('whitelist','custom')),
  reason text,
  created_by uuid REFERENCES tgmate.users(id),
  expires_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE CASCADE,
  CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE UNIQUE INDEX moderation_exemptions_active_uq
  ON tgmate.moderation_exemptions(tenant_id,group_config_id,telegram_user_id)
  WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- Message/event intake and rule catalogue
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_message_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  external_event_key text NOT NULL,
  telegram_message_id bigint,
  telegram_user_id bigint,
  event_type text NOT NULL
    CHECK (event_type IN ('message','edited_message','join','leave','service')),
  content_hash text,
  message_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  received_at timestamptz NOT NULL DEFAULT now(),
  message_at timestamptz,
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,group_config_id,external_event_key),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX moderation_message_events_user_time_idx
  ON tgmate.moderation_message_events(
    tenant_id,group_config_id,telegram_user_id,received_at DESC
  );

CREATE TABLE tgmate.moderation_event_processing (
  tenant_id uuid NOT NULL,
  message_event_id uuid NOT NULL,
  processing_status text NOT NULL DEFAULT 'received'
    CHECK (processing_status IN ('received','processing','processed','ignored','failed')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  locked_by uuid REFERENCES tgmate.worker_nodes(id),
  locked_until timestamptz,
  last_error_code text,
  processed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,message_event_id),
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT,
  CHECK ((locked_by IS NULL AND locked_until IS NULL)
      OR (locked_by IS NOT NULL AND locked_until IS NOT NULL))
);
CREATE INDEX moderation_event_processing_dispatch_idx
  ON tgmate.moderation_event_processing(processing_status,updated_at)
  WHERE processing_status IN ('received','failed','processing');

CREATE TABLE tgmate.moderation_rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  rule_code text NOT NULL,
  name text NOT NULL,
  rule_type text NOT NULL
    CHECK (rule_type IN ('keyword','regex','link','invite_link','flood','duplicate',
                         'spam','scam','malware_link','mention_spam','custom')),
  match_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  severity text NOT NULL DEFAULT 'medium'
    CHECK (severity IN ('low','medium','high','critical')),
  action text NOT NULL DEFAULT 'review'
    CHECK (action IN ('log','delete','warn','restrict','ban','review')),
  priority integer NOT NULL DEFAULT 100,
  enabled boolean NOT NULL DEFAULT true,
  created_by uuid REFERENCES tgmate.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,group_config_id,rule_code),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE CASCADE
);
CREATE INDEX moderation_rules_dispatch_idx
  ON tgmate.moderation_rules(tenant_id,group_config_id,enabled,priority);

CREATE TABLE tgmate.moderation_rule_matches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  message_event_id uuid NOT NULL,
  rule_id uuid NOT NULL,
  matched_excerpt text,
  match_details jsonb NOT NULL DEFAULT '{}'::jsonb,
  matched_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,message_event_id,rule_id),
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,rule_id)
    REFERENCES tgmate.moderation_rules(tenant_id,id) ON DELETE RESTRICT
);

-- One message event can create at most one violation, even if several rules match.
CREATE TABLE tgmate.moderation_violations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  message_event_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  severity text NOT NULL
    CHECK (severity IN ('low','medium','high','critical')),
  summary text NOT NULL,
  counted_in_window boolean NOT NULL DEFAULT false,
  window_started_at timestamptz,
  window_ends_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,message_event_id),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT,
  CHECK (
    (counted_in_window AND window_started_at IS NOT NULL AND window_ends_at IS NOT NULL)
    OR
    (NOT counted_in_window)
  )
);
CREATE INDEX moderation_violations_user_time_idx
  ON tgmate.moderation_violations(
    tenant_id,group_config_id,telegram_user_id,created_at DESC
  );

-- Current threshold window per user/group. The service updates this row
-- transactionally while holding a row lock; expired windows are reset, while
-- historical violations remain in moderation_violations for audit.
CREATE TABLE tgmate.moderation_violation_counters (
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  window_started_at timestamptz NOT NULL,
  window_ends_at timestamptz NOT NULL,
  violation_count integer NOT NULL DEFAULT 0 CHECK (violation_count >= 0),
  last_violation_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,group_config_id,telegram_user_id),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE CASCADE,
  CHECK (window_ends_at > window_started_at)
);

-- ---------------------------------------------------------------------------
-- AI assessments and human review queue. AI is advisory by default.
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_ai_assessments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  message_event_id uuid NOT NULL,
  model_provider text,
  model_name text,
  model_version text,
  category text NOT NULL
    CHECK (category IN ('clean','spam','scam','phishing','malware','harassment',
                        'adult','violence','flood','other')),
  confidence numeric(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  suggested_action text NOT NULL
    CHECK (suggested_action IN ('none','log','delete','warn','restrict','review')),
  explanation text,
  evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  assessed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX moderation_ai_assessments_event_idx
  ON tgmate.moderation_ai_assessments(tenant_id,message_event_id,assessed_at DESC);

CREATE TABLE tgmate.moderation_review_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  message_event_id uuid NOT NULL,
  violation_id uuid,
  review_type text NOT NULL
    CHECK (review_type IN ('ai_uncertain','high_risk','appeal','permission_failure',
                           'verification_manual','other')),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','approved','rejected','resolved','expired')),
  assigned_to uuid REFERENCES tgmate.users(id),
  decision text,
  decision_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,message_event_id,review_type),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,violation_id)
    REFERENCES tgmate.moderation_violations(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX moderation_review_queue_pending_idx
  ON tgmate.moderation_review_queue(tenant_id,status,created_at)
  WHERE status IN ('pending','claimed');

-- ---------------------------------------------------------------------------
-- Action execution ledger, evidence and appeals
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  message_event_id uuid,
  violation_id uuid,
  telegram_user_id bigint NOT NULL,
  action_type text NOT NULL
    CHECK (action_type IN ('delete_message','warn','restrict','unrestrict','ban','unban',
                           'request_review','log_only')),
  source text NOT NULL
    CHECK (source IN ('rule','ai','threshold','admin','verification')),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','permission_check','queued','executing','succeeded',
                      'failed','blocked','manual_review','cancelled')),
  permission_check_status text NOT NULL DEFAULT 'pending'
    CHECK (permission_check_status IN ('pending','allowed','denied','unknown')),
  idempotency_key text NOT NULL,
  reason text,
  requested_by uuid REFERENCES tgmate.users(id),
  executed_at timestamptz,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,idempotency_key),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,violation_id)
    REFERENCES tgmate.moderation_violations(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX moderation_actions_status_idx
  ON tgmate.moderation_actions(tenant_id,status,created_at);

CREATE OR REPLACE FUNCTION tgmate.guard_moderation_action()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ban_allowed boolean;
DECLARE v_bot_id uuid;
DECLARE v_destination_id uuid;
DECLARE membership text;
DECLARE can_send boolean;
DECLARE can_delete boolean;
DECLARE can_restrict boolean;
DECLARE can_ban boolean;
DECLARE permission_ok boolean := true;
BEGIN
  IF NEW.action_type='ban' THEN
    SELECT auto_ban_enabled INTO ban_allowed
    FROM tgmate.moderation_group_configs
    WHERE tenant_id=NEW.tenant_id AND id=NEW.group_config_id;

    IF ban_allowed IS DISTINCT FROM true AND NEW.requested_by IS NULL THEN
      RAISE EXCEPTION 'automatic ban is disabled for this group';
    END IF;
  END IF;

  -- Any action that calls Telegram must have a fresh allowed permission check.
  IF NEW.status IN ('queued','executing') AND
     NEW.action_type IN ('delete_message','warn','restrict','unrestrict','ban','unban') THEN

    SELECT c.telegram_bot_id,c.destination_id
      INTO v_bot_id,v_destination_id
    FROM tgmate.moderation_group_configs AS c
    WHERE c.tenant_id=NEW.tenant_id AND c.id=NEW.group_config_id;

    SELECT a.membership_status,a.can_send_messages,a.can_delete_messages,
           a.can_restrict_members,a.can_ban_members
      INTO membership,can_send,can_delete,can_restrict,can_ban
    FROM tgmate.bot_destination_access AS a
    WHERE a.tenant_id=NEW.tenant_id
      AND a.telegram_bot_id=v_bot_id
      AND a.destination_id=v_destination_id;

    IF membership IS DISTINCT FROM 'member' THEN
      RAISE EXCEPTION 'moderation Bot is not confirmed as a group member';
    END IF;

    permission_ok := CASE NEW.action_type
      WHEN 'delete_message' THEN COALESCE(can_delete,false)
      WHEN 'warn' THEN COALESCE(can_send,false)
      WHEN 'restrict' THEN COALESCE(can_restrict,false)
      WHEN 'unrestrict' THEN COALESCE(can_restrict,false)
      WHEN 'ban' THEN COALESCE(can_ban,false)
      WHEN 'unban' THEN COALESCE(can_ban,false)
      ELSE true
    END;

    IF NEW.permission_check_status IS DISTINCT FROM 'allowed'
       OR permission_ok IS DISTINCT FROM true THEN
      RAISE EXCEPTION 'required Telegram permission is not confirmed for action %',
        NEW.action_type;
    END IF;
  END IF;

  IF TG_OP='UPDATE'
     AND OLD.status IN ('succeeded','cancelled')
     AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'completed moderation action status is immutable';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER moderation_action_guard
  BEFORE INSERT OR UPDATE ON tgmate.moderation_actions
  FOR EACH ROW EXECUTE FUNCTION tgmate.guard_moderation_action();

CREATE TABLE tgmate.moderation_action_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  action_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  permission_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL
    CHECK (status IN ('started','succeeded','failed','blocked','unknown')),
  error_code text,
  error_detail text,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id,action_id,attempt_number),
  FOREIGN KEY (tenant_id,action_id)
    REFERENCES tgmate.moderation_actions(tenant_id,id) ON DELETE RESTRICT,
  CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE tgmate.moderation_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  message_event_id uuid,
  violation_id uuid,
  action_id uuid,
  evidence_type text NOT NULL
    CHECK (evidence_type IN ('message_snapshot','media_reference','ai_assessment',
                             'rule_match','admin_note','telegram_response')),
  object_vault_ref text,
  evidence_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_sha256 text,
  retention_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,message_event_id)
    REFERENCES tgmate.moderation_message_events(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,violation_id)
    REFERENCES tgmate.moderation_violations(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,action_id)
    REFERENCES tgmate.moderation_actions(tenant_id,id) ON DELETE RESTRICT,
  CHECK (retention_until IS NULL OR retention_until > created_at)
);
CREATE INDEX moderation_evidence_violation_idx
  ON tgmate.moderation_evidence(tenant_id,violation_id,created_at);

CREATE TABLE tgmate.moderation_appeals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  action_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  appeal_text text,
  status text NOT NULL DEFAULT 'submitted'
    CHECK (status IN ('submitted','under_review','accepted','rejected','withdrawn')),
  reviewer_user_id uuid REFERENCES tgmate.users(id),
  reviewer_note text,
  submitted_at timestamptz NOT NULL DEFAULT now(),
  reviewed_at timestamptz,
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,action_id,telegram_user_id),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,action_id)
    REFERENCES tgmate.moderation_actions(tenant_id,id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Welcome and verification lifecycle
-- ---------------------------------------------------------------------------

CREATE TABLE tgmate.moderation_welcome_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  join_event_key text NOT NULL,
  welcome_status text NOT NULL DEFAULT 'pending'
    CHECK (welcome_status IN ('pending','sent','failed','skipped')),
  welcome_message_ref text,
  sent_at timestamptz,
  verification_status text NOT NULL DEFAULT 'not_started'
    CHECK (verification_status IN ('not_started','pending','passed','failed','expired','manual')),
  success_message_status text NOT NULL DEFAULT 'not_configured'
    CHECK (success_message_status IN ('not_configured','pending','sent','failed','skipped')),
  success_message_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,group_config_id,join_event_key),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE tgmate.moderation_verification_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  group_config_id uuid NOT NULL,
  telegram_user_id bigint NOT NULL,
  join_event_key text NOT NULL,
  challenge_type text NOT NULL
    CHECK (challenge_type IN ('button','simple_question','admin_review','external_human_check')),
  challenge_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','passed','failed','expired','manual_review','cancelled')),
  attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts smallint NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  expires_at timestamptz NOT NULL,
  passed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,id),
  UNIQUE (tenant_id,group_config_id,join_event_key),
  FOREIGN KEY (tenant_id,group_config_id)
    REFERENCES tgmate.moderation_group_configs(tenant_id,id) ON DELETE RESTRICT,
  CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX moderation_verification_one_pending_user_uq
  ON tgmate.moderation_verification_sessions(tenant_id,group_config_id,telegram_user_id)
  WHERE status='pending';

CREATE TABLE tgmate.moderation_verification_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  verification_session_id uuid NOT NULL,
  attempt_number smallint NOT NULL CHECK (attempt_number > 0),
  response_fingerprint text,
  result text NOT NULL CHECK (result IN ('passed','failed','expired','manual')),
  attempted_at timestamptz NOT NULL DEFAULT now(),
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (tenant_id,verification_session_id,attempt_number),
  FOREIGN KEY (tenant_id,verification_session_id)
    REFERENCES tgmate.moderation_verification_sessions(tenant_id,id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Mutable updated_at triggers
-- ---------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'moderation_group_configs','moderation_group_members','moderation_rules',
    'moderation_violation_counters','moderation_event_processing','moderation_actions',
    'moderation_welcome_deliveries','moderation_verification_sessions'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON tgmate.%I '
      'FOR EACH ROW EXECUTE FUNCTION tgmate.touch_updated_at()',
      t || '_touch_updated_at',t
    );
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Append-only protections for evidence, event and decision history.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION tgmate.reject_moderation_history_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; add a new event instead', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER moderation_message_events_append_only
  BEFORE UPDATE OR DELETE ON tgmate.moderation_message_events
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_moderation_history_mutation();

CREATE TRIGGER moderation_rule_matches_append_only
  BEFORE UPDATE OR DELETE ON tgmate.moderation_rule_matches
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_moderation_history_mutation();

CREATE TRIGGER moderation_violations_append_only
  BEFORE UPDATE OR DELETE ON tgmate.moderation_violations
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_moderation_history_mutation();

CREATE TRIGGER moderation_evidence_append_only
  BEFORE UPDATE OR DELETE ON tgmate.moderation_evidence
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_moderation_history_mutation();

CREATE TRIGGER moderation_verification_attempts_append_only
  BEFORE UPDATE OR DELETE ON tgmate.moderation_verification_attempts
  FOR EACH ROW EXECUTE FUNCTION tgmate.reject_moderation_history_mutation();

-- ---------------------------------------------------------------------------
-- Tenant RLS on all tenant-owned tables. Global platform defaults are excluded.
-- ---------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'moderation_group_configs','moderation_group_members','moderation_exemptions',
    'moderation_message_events','moderation_event_processing',
    'moderation_rules','moderation_rule_matches',
    'moderation_violations','moderation_violation_counters',
    'moderation_ai_assessments','moderation_review_queue',
    'moderation_actions','moderation_action_attempts','moderation_evidence',
    'moderation_appeals','moderation_welcome_deliveries',
    'moderation_verification_sessions','moderation_verification_attempts'
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

-- Application services required (not implemented by SQL alone):
-- 1. On join: create idempotent welcome record, send welcome, then create
--    verification session and apply temporary restriction if permissions allow.
-- 2. Verify within timeout/max attempts; CAPTCHA/slider/third-party human checks
--    are escalated to manual review, never bypassed.
-- 3. On verification success: remove temporary restriction and send the
--    configured second message once. On failure/timeout, apply configured
--    safe policy only if Bot permissions are present.
-- 4. Deduplicate each Telegram message/update before rule evaluation.
--    Multiple rule matches for one event create one violation only.
-- 5. Update violation counter in a transaction with row lock. If the current
--    window expired, start a new window; old violations remain in history but
--    do not count toward the new window.
-- 6. Exempt owner/admin/whitelist users before applying moderation actions.
--    Refresh Telegram admin status; do not rely indefinitely on stale cache.
-- 7. Before delete/restrict/ban, re-check Bot permissions. If delete permission
--    is absent, only log and notify. Automatic ban requires explicit enablement.
-- 8. AI output is advisory by default. Enforce mode still obeys configured
--    confidence thresholds, permissions, human-review policy and auto-ban OFF.
-- 9. Store evidence in encrypted object storage; use short, configurable
--    retention and least-privilege access.
-- 10. Use outbox/idempotency for notifications and Telegram API actions.
