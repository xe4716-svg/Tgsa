BEGIN;

-- Recover expired claims only while holding the current account lease. A command
-- that may have crossed the Telegram send boundary becomes UNKNOWN, never queued.
CREATE FUNCTION public.recover_expired_gateway_account_commands(
    p_account_id UUID, p_worker_id TEXT, p_fencing_token BIGINT
) RETURNS TABLE(requeued_before_dispatch INTEGER, marked_unknown INTEGER)
LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_requeued INTEGER := 0; v_unknown INTEGER := 0;
BEGIN
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    IF v_tenant IS NULL OR p_account_id IS NULL OR p_worker_id IS NULL
       OR length(p_worker_id) NOT BETWEEN 1 AND 150 OR p_fencing_token <= 0 THEN
        RAISE EXCEPTION 'invalid gateway recovery request' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.account_gateway_leases l
        WHERE l.tenant_id=v_tenant AND l.account_id=p_account_id
          AND l.worker_id=p_worker_id AND l.fencing_token=p_fencing_token
          AND l.lease_expires_at>clock_timestamp()) THEN
        RAISE EXCEPTION 'current gateway lease required' USING ERRCODE = '42501';
    END IF;
    WITH recovered AS (
        UPDATE public.gateway_account_commands c
        SET status=CASE WHEN c.dispatch_started_at IS NULL THEN 'queued' ELSE 'unknown' END,
            claim_worker_id=NULL,claim_fencing_token=NULL,claim_until=NULL,
            error_code=CASE WHEN c.dispatch_started_at IS NULL THEN NULL ELSE 'WORKER_LOST_AFTER_DISPATCH' END,
            result=CASE WHEN c.dispatch_started_at IS NULL THEN NULL ELSE '{}'::jsonb END,
            completed_at=CASE WHEN c.dispatch_started_at IS NULL THEN NULL ELSE clock_timestamp() END,
            available_at=CASE WHEN c.dispatch_started_at IS NULL THEN clock_timestamp() ELSE c.available_at END,
            updated_at=clock_timestamp()
        WHERE c.tenant_id=v_tenant AND c.account_id=p_account_id
          AND c.status='claimed' AND c.claim_until<=clock_timestamp()
        RETURNING c.id,c.tenant_id,c.account_id,c.status
    ), logged AS (
        INSERT INTO public.gateway_account_command_events(
            tenant_id,command_id,account_id,transition,actor_class,metadata
        ) SELECT tenant_id,id,account_id,
            CASE status WHEN 'queued' THEN 'requeued' ELSE 'unknown' END,
            'gateway',jsonb_build_object('reason_code',
                CASE status WHEN 'queued' THEN 'CLAIM_EXPIRED_BEFORE_DISPATCH' ELSE 'WORKER_LOST_AFTER_DISPATCH' END)
          FROM recovered
    )
    SELECT count(*) FILTER (WHERE status='queued')::integer,
           count(*) FILTER (WHERE status='unknown')::integer
      INTO v_requeued,v_unknown FROM recovered;
    RETURN QUERY SELECT v_requeued,v_unknown;
END;
$$;
REVOKE ALL ON FUNCTION public.recover_expired_gateway_account_commands(uuid,text,bigint) FROM PUBLIC;
COMMIT;
