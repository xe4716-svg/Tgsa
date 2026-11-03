-- Worker-only bridge: validate/lock the existing mapping and both tenant states
-- during each short business transaction. Never grant identity table writes.
CREATE FUNCTION public.lock_business_worker_tenant(business_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE root_id uuid; root_state text; business_state text;
BEGIN
 IF business_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
  RETURN false;
 END IF;
 SELECT tenant_id INTO root_id FROM public.integration_tenant_map WHERE business_tenant_id=business_id;
 IF root_id IS NULL THEN RETURN false; END IF;
 SELECT status INTO root_state FROM public.tenants WHERE id=root_id FOR SHARE;
 -- Recheck/lock mapping after the potentially blocking tenant lock.
 PERFORM 1 FROM public.integration_tenant_map
   WHERE tenant_id=root_id AND business_tenant_id=business_id FOR SHARE;
 IF NOT FOUND OR root_state <> 'ACTIVE' THEN RETURN false; END IF;
 -- The business schema is installed after root migrations; defer relation
 -- resolution until execution so root-only migration remains supported.
 EXECUTE 'SELECT status FROM tgmate.tenants WHERE id=$1 FOR SHARE'
   INTO business_state USING business_id;
 RETURN COALESCE(business_state='active',false);
END;
$$;
REVOKE ALL ON FUNCTION public.lock_business_worker_tenant(uuid) FROM PUBLIC;
