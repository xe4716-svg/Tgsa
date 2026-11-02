BEGIN;
CREATE TABLE tgmate.relay_privacy_notices (
 tenant_id uuid NOT NULL,
 conversation_id uuid NOT NULL,
 version text NOT NULL,
 outbox_id uuid,
 confirmed_at timestamptz,
 remote_message_id bigint CHECK(remote_message_id>0),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,conversation_id,version),
 CHECK(outbox_id IS NOT NULL OR confirmed_at IS NOT NULL),
 CHECK((confirmed_at IS NULL)=(remote_message_id IS NULL)),
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES tgmate.relay_conversations(tenant_id,id),
 FOREIGN KEY(tenant_id,outbox_id) REFERENCES tgmate.outbox_events(tenant_id,id)
);
ALTER TABLE tgmate.relay_privacy_notices ENABLE ROW LEVEL SECURITY;
ALTER TABLE tgmate.relay_privacy_notices FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON tgmate.relay_privacy_notices USING(tenant_id=tgmate.current_tenant_id()) WITH CHECK(tenant_id=tgmate.current_tenant_id());
COMMIT;
