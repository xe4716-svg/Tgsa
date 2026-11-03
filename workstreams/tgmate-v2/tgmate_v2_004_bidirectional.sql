-- 004 customer-service relay / PostgreSQL 15+ / apply once after 001-003
-- SQL source only: execution evidence is recorded separately in CLEAN_REBUILD_RESULTS.json.
BEGIN;
CREATE TABLE tgmate.relay_configs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id uuid NOT NULL,
 robot_instance_id uuid NOT NULL,
 telegram_bot_id uuid NOT NULL,
 executor_binding_id uuid NOT NULL,
 binding_purpose text NOT NULL DEFAULT 'send' CHECK(binding_purpose='send'),
 operator_user_id bigint NOT NULL CHECK(operator_user_id>0),
 status text NOT NULL DEFAULT 'paused' CHECK(status IN ('active','paused','revoked')),
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,robot_instance_id),
 FOREIGN KEY(tenant_id,robot_instance_id) REFERENCES tgmate.robot_instances(tenant_id,id),
 FOREIGN KEY(tenant_id,telegram_bot_id) REFERENCES tgmate.telegram_bots(tenant_id,id),
 FOREIGN KEY(tenant_id,executor_binding_id,robot_instance_id,telegram_bot_id,binding_purpose)
 REFERENCES tgmate.executor_bindings(tenant_id,id,robot_instance_id,telegram_bot_id,purpose)
);
CREATE FUNCTION tgmate.guard_relay_config() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM tgmate.robot_instances WHERE tenant_id=NEW.tenant_id
 AND id=NEW.robot_instance_id AND product_type_code='bidirectional') THEN
 RAISE EXCEPTION 'relay requires bidirectional robot'; END IF;
 IF TG_OP='UPDATE' THEN
 IF (NEW.tenant_id,NEW.robot_instance_id,NEW.telegram_bot_id,NEW.executor_binding_id)
 IS DISTINCT FROM (OLD.tenant_id,OLD.robot_instance_id,OLD.telegram_bot_id,OLD.executor_binding_id) THEN
 RAISE EXCEPTION 'relay identity is immutable'; END IF;
 IF (NEW.status,NEW.operator_user_id) IS DISTINCT FROM (OLD.status,OLD.operator_user_id) THEN
 NEW.generation:=OLD.generation+1;
 ELSIF NEW.generation<>OLD.generation THEN RAISE EXCEPTION 'generation is managed'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER relay_config_guard BEFORE INSERT OR UPDATE ON tgmate.relay_configs
FOR EACH ROW EXECUTE FUNCTION tgmate.guard_relay_config();
CREATE TABLE tgmate.relay_conversations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
 config_id uuid NOT NULL, customer_user_id bigint NOT NULL CHECK(customer_user_id>0),
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','paused','blocked','closed')),
 initiated_at timestamptz NOT NULL, privacy_notice_version text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,config_id,customer_user_id),
 UNIQUE(tenant_id,id,config_id),
 FOREIGN KEY(tenant_id,config_id) REFERENCES tgmate.relay_configs(tenant_id,id)
);
CREATE FUNCTION tgmate.guard_relay_conversation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.tenant_id,NEW.config_id,NEW.customer_user_id,NEW.initiated_at,NEW.privacy_notice_version)
 IS DISTINCT FROM (OLD.tenant_id,OLD.config_id,OLD.customer_user_id,OLD.initiated_at,OLD.privacy_notice_version)
 THEN RAISE EXCEPTION 'conversation identity is immutable'; END IF;
 IF NEW.status IS DISTINCT FROM OLD.status THEN NEW.generation:=OLD.generation+1;
 ELSIF NEW.generation<>OLD.generation THEN RAISE EXCEPTION 'conversation generation is managed'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER relay_conversation_guard BEFORE UPDATE ON tgmate.relay_conversations
FOR EACH ROW EXECUTE FUNCTION tgmate.guard_relay_conversation();
-- Domain operation record, consumed through the shared outbox; no second global queue.
CREATE TABLE tgmate.relay_operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
 config_id uuid NOT NULL, conversation_id uuid NOT NULL,
 event_key text NOT NULL, direction text NOT NULL CHECK(direction IN ('to_operator','to_customer')),
 source_chat_id bigint NOT NULL, target_chat_id bigint NOT NULL,
 conversation_generation bigint NOT NULL CHECK(conversation_generation>0), source_message_id bigint NOT NULL CHECK(source_message_id>0),
 reply_to_operation_id uuid,
 payload_vault_ref text NOT NULL,
 generation bigint NOT NULL CHECK(generation>0),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','succeeded','retry_wait','uncertain','failed','cancelled')),
 target_message_id bigint CHECK(target_message_id>0),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,id,conversation_id),
 UNIQUE(tenant_id,config_id,event_key),
 FOREIGN KEY(tenant_id,conversation_id,config_id) REFERENCES tgmate.relay_conversations(tenant_id,id,config_id),
 FOREIGN KEY(tenant_id,reply_to_operation_id,conversation_id) REFERENCES tgmate.relay_operations(tenant_id,id,conversation_id),
 CHECK(direction<>'to_customer' OR reply_to_operation_id IS NOT NULL),
 CHECK(status<>'succeeded' OR target_message_id IS NOT NULL)
);
CREATE UNIQUE INDEX relay_operator_reply_lookup_uq ON tgmate.relay_operations(tenant_id,config_id,target_chat_id,target_message_id) WHERE direction='to_operator' AND target_message_id IS NOT NULL;
CREATE INDEX relay_operations_conversation_idx ON tgmate.relay_operations(tenant_id,conversation_id,created_at);
CREATE FUNCTION tgmate.guard_relay_operation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c record; v record;
BEGIN
 SELECT * INTO STRICT c FROM tgmate.relay_configs WHERE tenant_id=NEW.tenant_id AND id=NEW.config_id;
 SELECT * INTO STRICT v FROM tgmate.relay_conversations WHERE tenant_id=NEW.tenant_id AND id=NEW.conversation_id;
 IF TG_OP='INSERT' AND (NEW.status<>'pending' OR NEW.target_message_id IS NOT NULL) THEN
 RAISE EXCEPTION 'new operation must be pending'; END IF;
 IF TG_OP='UPDATE' THEN
 IF (NEW.tenant_id,NEW.config_id,NEW.conversation_id,NEW.direction,NEW.source_chat_id,NEW.source_message_id,NEW.event_key,NEW.reply_to_operation_id,NEW.generation,NEW.payload_vault_ref,NEW.target_chat_id,NEW.conversation_generation)
 IS DISTINCT FROM (OLD.tenant_id,OLD.config_id,OLD.conversation_id,OLD.direction,OLD.source_chat_id,OLD.source_message_id,OLD.event_key,OLD.reply_to_operation_id,OLD.generation,OLD.payload_vault_ref,OLD.target_chat_id,OLD.conversation_generation)
 THEN RAISE EXCEPTION 'operation routing is immutable'; END IF;
 IF NEW.status<>OLD.status AND NOT (
 (OLD.status='pending' AND NEW.status IN ('sending','cancelled','failed')) OR
 (OLD.status='sending' AND NEW.status IN ('succeeded','retry_wait','uncertain','failed')) OR
 (OLD.status='retry_wait' AND NEW.status IN ('sending','cancelled','failed')) OR
 (OLD.status='uncertain' AND NEW.status IN ('succeeded','failed','cancelled'))
 ) THEN RAISE EXCEPTION 'invalid operation transition'; END IF;
 IF OLD.target_message_id IS NOT NULL AND NEW.target_message_id IS DISTINCT FROM OLD.target_message_id THEN
 RAISE EXCEPTION 'confirmed target is immutable'; END IF;
 END IF;
 IF NEW.status IN ('pending','sending','retry_wait') THEN
 IF c.status<>'active' OR v.status<>'open' OR NEW.generation<>c.generation OR NEW.conversation_generation<>v.generation THEN
 RAISE EXCEPTION 'relay paused, blocked or stale'; END IF;
 IF NOT EXISTS(SELECT 1 FROM tgmate.executor_bindings WHERE tenant_id=c.tenant_id AND id=c.executor_binding_id AND status='active')
 OR NOT EXISTS(SELECT 1 FROM tgmate.telegram_bots WHERE tenant_id=c.tenant_id AND id=c.telegram_bot_id AND status='active') THEN
 RAISE EXCEPTION 'executor unavailable'; END IF;
 END IF;
 IF TG_OP='INSERT' AND NEW.target_chat_id <> (CASE WHEN NEW.direction='to_operator' THEN c.operator_user_id ELSE v.customer_user_id END) THEN
 RAISE EXCEPTION 'invalid target chat'; END IF;
 IF (TG_OP='INSERT' OR NEW.status IN ('pending','sending','retry_wait')) AND NEW.source_chat_id <> (CASE WHEN NEW.direction='to_operator' THEN v.customer_user_id ELSE c.operator_user_id END) THEN
 RAISE EXCEPTION 'invalid source chat'; END IF;
 IF NEW.direction='to_customer' AND NOT EXISTS(SELECT 1 FROM tgmate.relay_operations
 WHERE tenant_id=NEW.tenant_id AND id=NEW.reply_to_operation_id AND conversation_id=NEW.conversation_id
 AND direction='to_operator' AND status='succeeded' AND generation=NEW.generation) THEN
 RAISE EXCEPTION 'reply must reference delivered customer message'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER relay_operation_guard BEFORE INSERT OR UPDATE ON tgmate.relay_operations
FOR EACH ROW EXECUTE FUNCTION tgmate.guard_relay_operation();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['relay_configs','relay_conversations','relay_operations'] LOOP
 EXECUTE format('ALTER TABLE tgmate.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE tgmate.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY tenant_isolation ON tgmate.%I USING (tenant_id=tgmate.current_tenant_id()) WITH CHECK (tenant_id=tgmate.current_tenant_id())',t);
 EXECUTE format('REVOKE ALL ON tgmate.%I FROM PUBLIC',t);
 END LOOP;
END $$;
COMMIT;


