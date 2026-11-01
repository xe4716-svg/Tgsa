BEGIN;
CREATE FUNCTION tgmate.invalidate_relay_operator_authorization()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE tgmate.relay_operator_authorizations
 SET confirmed_at=NULL,expires_at=now()
 WHERE tenant_id=NEW.tenant_id AND config_id=NEW.id;
 RETURN NEW;
END $$;
CREATE TRIGGER relay_operator_identity_changed
AFTER UPDATE OF operator_user_id ON tgmate.relay_configs
FOR EACH ROW WHEN (OLD.operator_user_id IS DISTINCT FROM NEW.operator_user_id)
EXECUTE FUNCTION tgmate.invalidate_relay_operator_authorization();
COMMIT;
