-- Apply once after 004a. Original 001-003 files remain unchanged.
BEGIN;
CREATE OR REPLACE FUNCTION tgmate.validate_moderation_robot_type()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_type text;
DECLARE binding_status text;
BEGIN
  -- Allow only a control-only disable, even after executor loss.
  -- Identity, tenant, policy and binding edits still require all original guards.
  IF TG_OP='UPDATE' AND NOT NEW.enabled
     AND (to_jsonb(NEW)-'enabled'-'updated_at') = (to_jsonb(OLD)-'enabled'-'updated_at') THEN
    RETURN NEW;
  END IF;
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
COMMIT;
