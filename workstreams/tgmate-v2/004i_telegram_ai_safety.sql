-- Forward-only compliance guard. Keep historical assessments for audit.
BEGIN;
UPDATE tgmate.moderation_platform_defaults SET ai_mode='off' WHERE ai_mode<>'off';
UPDATE tgmate.moderation_group_configs SET ai_mode='off' WHERE ai_mode<>'off';
ALTER TABLE tgmate.moderation_platform_defaults ALTER COLUMN ai_mode SET DEFAULT 'off';
ALTER TABLE tgmate.moderation_group_configs ALTER COLUMN ai_mode SET DEFAULT 'off';
ALTER TABLE tgmate.moderation_platform_defaults ADD CONSTRAINT telegram_ai_platform_off CHECK (ai_mode='off');
ALTER TABLE tgmate.moderation_group_configs ADD CONSTRAINT telegram_ai_group_off CHECK (ai_mode='off');
CREATE FUNCTION tgmate.reject_telegram_ai_assessment() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 RAISE EXCEPTION 'Telegram-sourced content cannot be processed by AI' USING ERRCODE='23514';
END $$;
REVOKE ALL ON FUNCTION tgmate.reject_telegram_ai_assessment() FROM PUBLIC;
CREATE TRIGGER telegram_ai_assessment_disabled
BEFORE INSERT OR UPDATE ON tgmate.moderation_ai_assessments
FOR EACH ROW EXECUTE FUNCTION tgmate.reject_telegram_ai_assessment();
COMMIT;
