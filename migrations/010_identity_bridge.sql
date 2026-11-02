BEGIN;
CREATE TABLE public.integration_tenant_map (
 tenant_id uuid PRIMARY KEY REFERENCES public.tenants(id) ON DELETE CASCADE,
 business_tenant_id uuid NOT NULL UNIQUE,
 UNIQUE(tenant_id,business_tenant_id)
);
CREATE TABLE public.integration_user_map (
 user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
 business_user_id uuid NOT NULL UNIQUE
);
CREATE TABLE public.integration_member_map (
 tenant_id uuid NOT NULL,
 user_id uuid NOT NULL REFERENCES public.integration_user_map(user_id) ON DELETE CASCADE,
 business_tenant_id uuid NOT NULL,
 PRIMARY KEY(tenant_id,user_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES public.tenant_memberships(tenant_id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(tenant_id,business_tenant_id) REFERENCES public.integration_tenant_map(tenant_id,business_tenant_id) ON DELETE CASCADE
);
ALTER TABLE public.integration_tenant_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integration_tenant_map FORCE ROW LEVEL SECURITY;
ALTER TABLE public.integration_member_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integration_member_map FORCE ROW LEVEL SECURITY;
ALTER TABLE public.integration_user_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integration_user_map FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON public.integration_tenant_map USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
CREATE POLICY tenant_scope ON public.integration_member_map USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
CREATE POLICY mapped_user_scope ON public.integration_user_map USING (EXISTS(SELECT 1 FROM public.integration_member_map m WHERE m.user_id=integration_user_map.user_id));
REVOKE ALL ON public.integration_tenant_map,public.integration_user_map,public.integration_member_map FROM PUBLIC,tgs_runtime;
GRANT SELECT ON public.integration_tenant_map,public.integration_user_map,public.integration_member_map TO tgs_runtime;
COMMIT;
