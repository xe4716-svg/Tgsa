BEGIN;

-- Gateway polls only the account it currently owns. This avoids granting the
-- Gateway process broad command-table SELECT access merely to discover work.
CREATE FUNCTION public.claim_next_gateway_account_command(
    p_account_id UUID,
    p_worker_id TEXT,
    p_fencing_token BIGINT,
    p_claim_seconds INTEGER DEFAULT 15
) RETURNS SETOF public.gateway_account_commands
LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID;
BEGIN
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    IF v_tenant IS NULL OR p_account_id IS NULL OR p_worker_id IS NULL
       OR length(p_worker_id) NOT BETWEEN 1 AND 150 OR p_fencing_token <= 0
       OR p_claim_seconds NOT BETWEEN 5 AND 60 THEN
        RAISE EXCEPTION 'invalid gateway claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH eligible AS (
        SELECT c.id FROM public.gateway_account_commands c
        JOIN public.account_gateway_routes r
          ON r.tenant_id=c.tenant_id AND r.account_id=c.account_id
        JOIN public.account_gateway_leases l
          ON l.tenant_id=c.tenant_id AND l.account_id=c.account_id
        JOIN public.telegram_accounts a
          ON a.tenant_id=c.tenant_id AND a.id=c.account_id
        JOIN public.tenants t ON t.id=c.tenant_id
        WHERE c.tenant_id=v_tenant AND c.account_id=p_account_id
          AND c.status='queued' AND c.available_at<=clock_timestamp()
          AND r.enabled AND r.generation=c.route_generation
          AND l.worker_id=p_worker_id AND l.fencing_token=p_fencing_token
          AND l.lease_expires_at>clock_timestamp()
          AND a.status IN ('AUTHORIZED','ACTIVE') AND t.status='ACTIVE'
        ORDER BY c.available_at,c.created_at,c.id
        FOR UPDATE OF c SKIP LOCKED LIMIT 1
    ), claimed AS (
        UPDATE public.gateway_account_commands c
        SET status='claimed',claim_worker_id=p_worker_id,
            claim_fencing_token=p_fencing_token,
            claim_until=clock_timestamp()+p_claim_seconds*interval '1 second',
            updated_at=clock_timestamp()
        FROM eligible e WHERE c.id=e.id
        RETURNING c.*
    ), logged AS (
        INSERT INTO public.gateway_account_command_events(
            tenant_id,command_id,account_id,transition,actor_class,metadata
        ) SELECT tenant_id,id,account_id,'claimed','gateway',
            jsonb_build_object('reason_code','LEASE_FENCED') FROM claimed
    ) SELECT claimed.* FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_next_gateway_account_command(uuid,text,bigint,integer) FROM PUBLIC;
COMMIT;
