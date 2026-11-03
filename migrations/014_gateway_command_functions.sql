BEGIN;

-- Atomic, tenant-scoped command transitions. EXECUTE remains revoked until
-- dedicated submitter/Gateway roles are provisioned and integration-tested.

CREATE FUNCTION public.submit_gateway_account_command(
    p_account_id UUID,
    p_route_generation BIGINT,
    p_operation_key UUID,
    p_destination_peer_id BIGINT,
    p_text TEXT
) RETURNS UUID
LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_id UUID; v_existing public.gateway_account_commands%ROWTYPE;
BEGIN
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    IF v_tenant IS NULL OR p_route_generation <= 0 OR p_operation_key IS NULL
       OR p_destination_peer_id = 0 OR p_text IS NULL OR length(p_text) NOT BETWEEN 1 AND 4096 THEN
        RAISE EXCEPTION 'invalid gateway command request' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.account_gateway_routes r
        JOIN public.telegram_accounts a ON a.tenant_id=r.tenant_id AND a.id=r.account_id
        JOIN public.tenants t ON t.id=r.tenant_id
        WHERE r.tenant_id=v_tenant AND r.account_id=p_account_id
          AND r.enabled AND r.generation=p_route_generation
          AND a.status IN ('AUTHORIZED','ACTIVE') AND t.status='ACTIVE'
    ) THEN
        RAISE EXCEPTION 'account route unavailable' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.gateway_account_commands(
        tenant_id,account_id,route_generation,operation_key,operation_kind,
        destination_peer_id,payload,status
    ) VALUES (v_tenant,p_account_id,p_route_generation,p_operation_key,
        'preflight_send_text',p_destination_peer_id,jsonb_build_object('text',p_text),'queued')
    ON CONFLICT (tenant_id,account_id,operation_key) DO NOTHING
    RETURNING id INTO v_id;
    IF v_id IS NOT NULL THEN
        INSERT INTO public.gateway_account_command_events(
            tenant_id,command_id,account_id,transition,actor_class,metadata
        ) VALUES (v_tenant,v_id,p_account_id,'submitted','business_worker','{}'::jsonb);
        RETURN v_id;
    END IF;
    SELECT * INTO STRICT v_existing FROM public.gateway_account_commands
      WHERE tenant_id=v_tenant AND account_id=p_account_id AND operation_key=p_operation_key;
    IF v_existing.route_generation<>p_route_generation
       OR v_existing.operation_kind<>'preflight_send_text'
       OR v_existing.destination_peer_id<>p_destination_peer_id
       OR v_existing.payload<>jsonb_build_object('text',p_text) THEN
        RAISE EXCEPTION 'idempotency key reused with different command' USING ERRCODE = '23505';
    END IF;
    RETURN v_existing.id;
END;
$$;

CREATE FUNCTION public.claim_gateway_account_command(
    p_command_id UUID,
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
    IF v_tenant IS NULL OR p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 150
       OR p_fencing_token <= 0 OR p_claim_seconds NOT BETWEEN 5 AND 60 THEN
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
        WHERE c.id=p_command_id AND c.tenant_id=v_tenant
          AND c.status='queued' AND c.available_at<=clock_timestamp()
          AND r.enabled AND r.generation=c.route_generation
          AND l.worker_id=p_worker_id AND l.fencing_token=p_fencing_token
          AND l.lease_expires_at>clock_timestamp()
          AND a.status IN ('AUTHORIZED','ACTIVE') AND t.status='ACTIVE'
        FOR UPDATE OF c SKIP LOCKED
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

CREATE FUNCTION public.mark_gateway_account_command_dispatch_started(
    p_command_id UUID, p_worker_id TEXT, p_fencing_token BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_count INTEGER;
BEGIN
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    UPDATE public.gateway_account_commands c
    SET dispatch_started_at=clock_timestamp(),updated_at=clock_timestamp()
    WHERE c.id=p_command_id AND c.tenant_id=v_tenant AND c.status='claimed'
      AND c.claim_worker_id=p_worker_id AND c.claim_fencing_token=p_fencing_token
      AND c.claim_until>clock_timestamp()
      AND EXISTS (SELECT 1 FROM public.account_gateway_leases l
        WHERE l.tenant_id=c.tenant_id AND l.account_id=c.account_id
          AND l.worker_id=p_worker_id AND l.fencing_token=p_fencing_token
          AND l.lease_expires_at>clock_timestamp())
      AND EXISTS (SELECT 1 FROM public.account_gateway_routes r
        WHERE r.tenant_id=c.tenant_id AND r.account_id=c.account_id
          AND r.enabled AND r.generation=c.route_generation)
    RETURNING 1 INTO v_count;
    IF v_count=1 THEN
        INSERT INTO public.gateway_account_command_events(
            tenant_id,command_id,account_id,transition,actor_class,metadata
        ) SELECT tenant_id,id,account_id,'dispatch_started','gateway','{}'::jsonb
          FROM public.gateway_account_commands WHERE id=p_command_id AND tenant_id=v_tenant;
        RETURN TRUE;
    END IF;
    RETURN FALSE;
END;
$$;

CREATE FUNCTION public.finish_gateway_account_command(
    p_command_id UUID, p_worker_id TEXT, p_fencing_token BIGINT,
    p_status TEXT, p_remote_message_id BIGINT DEFAULT NULL,
    p_error_code TEXT DEFAULT NULL, p_retry_after INTEGER DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_account UUID; v_transition TEXT; v_meta JSONB; v_count INTEGER;
BEGIN
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    IF p_status NOT IN ('succeeded','rejected','flood_wait','unknown') THEN
        RAISE EXCEPTION 'invalid terminal command status' USING ERRCODE = '22023';
    END IF;
    IF p_status='succeeded' AND (p_remote_message_id IS NULL OR p_error_code IS NOT NULL OR p_retry_after IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid success result' USING ERRCODE = '22023';
    ELSIF p_status='flood_wait' AND (p_retry_after IS NULL OR p_retry_after<1 OR p_error_code IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid flood wait result' USING ERRCODE = '22023';
    ELSIF p_status IN ('rejected','unknown') AND (p_error_code IS NULL OR p_error_code !~ '^[A-Z][A-Z0-9_]{0,63}$' OR p_retry_after IS NOT NULL OR p_remote_message_id IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid error result' USING ERRCODE = '22023';
    END IF;
    UPDATE public.gateway_account_commands c
    SET status=p_status,
        result=CASE WHEN p_status='succeeded' THEN jsonb_build_object('remote_message_id',p_remote_message_id)
                    WHEN p_status='flood_wait' THEN jsonb_build_object('retry_after',p_retry_after)
                    ELSE '{}'::jsonb END,
        error_code=p_error_code,completed_at=clock_timestamp(),updated_at=clock_timestamp(),
        claim_until=NULL
    WHERE c.id=p_command_id AND c.tenant_id=v_tenant AND c.status='claimed'
      AND c.claim_worker_id=p_worker_id AND c.claim_fencing_token=p_fencing_token
      AND c.claim_until>clock_timestamp()
      AND EXISTS (SELECT 1 FROM public.account_gateway_leases l
        WHERE l.tenant_id=c.tenant_id AND l.account_id=c.account_id
          AND l.worker_id=p_worker_id AND l.fencing_token=p_fencing_token
          AND l.lease_expires_at>clock_timestamp())
    RETURNING c.account_id INTO v_account;
    IF v_account IS NULL THEN RETURN FALSE; END IF;
    v_transition := CASE p_status WHEN 'succeeded' THEN 'succeeded' WHEN 'rejected' THEN 'rejected' WHEN 'flood_wait' THEN 'flood_wait' ELSE 'unknown' END;
    v_meta := CASE p_status WHEN 'succeeded' THEN jsonb_build_object('remote_message_id',p_remote_message_id)
               WHEN 'flood_wait' THEN jsonb_build_object('retry_after',p_retry_after)
               ELSE jsonb_build_object('reason_code',p_error_code) END;
    INSERT INTO public.gateway_account_command_events(
        tenant_id,command_id,account_id,transition,actor_class,metadata
    ) VALUES (v_tenant,p_command_id,v_account,v_transition,'gateway',v_meta);
    RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.submit_gateway_account_command(uuid,bigint,uuid,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_gateway_account_command(uuid,text,bigint,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mark_gateway_account_command_dispatch_started(uuid,text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.finish_gateway_account_command(uuid,text,bigint,text,bigint,text,integer) FROM PUBLIC;

COMMIT;
