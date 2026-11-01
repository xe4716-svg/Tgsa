-- Runtime support for 004; apply once after 004, no remote services involved.
BEGIN;
ALTER TABLE tgmate.outbox_events ADD CONSTRAINT outbox_events_tenant_id_id_uq UNIQUE(tenant_id,id);
CREATE TABLE tgmate.relay_payloads (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
 ciphertext text NOT NULL, expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id), CHECK(expires_at>created_at)
);
CREATE TABLE tgmate.relay_runtime_links (
 tenant_id uuid NOT NULL, operation_id uuid NOT NULL, task_id uuid NOT NULL,
 outbox_id uuid NOT NULL, payload_id uuid NOT NULL,
 PRIMARY KEY(tenant_id,operation_id), UNIQUE(tenant_id,outbox_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES tgmate.relay_operations(tenant_id,id),
 FOREIGN KEY(tenant_id,task_id) REFERENCES tgmate.task_registry(tenant_id,id),
 FOREIGN KEY(tenant_id,payload_id) REFERENCES tgmate.relay_payloads(tenant_id,id),
 FOREIGN KEY(tenant_id,outbox_id) REFERENCES tgmate.outbox_events(tenant_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['relay_payloads','relay_runtime_links'] LOOP
 EXECUTE format('ALTER TABLE tgmate.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE tgmate.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY tenant_isolation ON tgmate.%I USING (tenant_id=tgmate.current_tenant_id()) WITH CHECK (tenant_id=tgmate.current_tenant_id())',t);
 EXECUTE format('REVOKE ALL ON tgmate.%I FROM PUBLIC',t);
 END LOOP;
END $$;
COMMIT;

