BEGIN;
-- These are capability groups, never application logins or members of tgs_runtime.
DO $$ DECLARE role_name text; BEGIN
 FOREACH role_name IN ARRAY ARRAY['tgs_command_owner','tgs_command_submitter','tgs_command_executor'] LOOP
  IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname=role_name) THEN
   EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',role_name);
  END IF;
  IF EXISTS(SELECT FROM pg_roles WHERE rolname=role_name AND
    (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole)) THEN
   RAISE EXCEPTION 'Unsafe existing command capability role';
  END IF;
 END LOOP;
END $$;
CREATE TABLE public.gateway_command_principals (
 login_name name NOT NULL, tenant_id uuid NOT NULL REFERENCES public.tenants(id),
 capability text NOT NULL CHECK(capability IN ('submit','execute')),
 PRIMARY KEY(login_name,tenant_id,capability)
);
REVOKE ALL ON public.gateway_command_principals FROM PUBLIC,tgs_runtime;
GRANT USAGE ON SCHEMA public TO tgs_command_owner,tgs_command_submitter,tgs_command_executor;
GRANT SELECT ON public.gateway_command_principals,public.integration_tenant_map,
 public.tenants,public.telegram_accounts,public.account_gateway_routes,public.account_gateway_leases
 TO tgs_command_owner;
-- Row locks require UPDATE privileges, but no such privilege is given to callers.
GRANT UPDATE(id) ON public.tenants,public.telegram_accounts TO tgs_command_owner;
GRANT UPDATE(account_id) ON public.account_gateway_routes,public.account_gateway_leases TO tgs_command_owner;
GRANT SELECT,INSERT,UPDATE ON public.gateway_account_commands TO tgs_command_owner;
GRANT SELECT,INSERT ON public.gateway_account_command_events TO tgs_command_owner;
GRANT USAGE ON SEQUENCE public.gateway_account_command_events_id_seq TO tgs_command_owner;
GRANT EXECUTE ON FUNCTION public.lock_business_worker_tenant(uuid) TO tgs_command_owner;
CREATE POLICY command_owner_map ON public.integration_tenant_map TO tgs_command_owner USING(true);

CREATE FUNCTION public.authorize_gateway_command(p_capability text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE requested uuid; root_id uuid;
BEGIN
 requested:=NULLIF(current_setting('app.tenant_id',true),'')::uuid;
 IF p_capability='submit' THEN
  SELECT tenant_id INTO root_id FROM public.integration_tenant_map WHERE business_tenant_id=requested;
 ELSE root_id:=requested;
 END IF;
 IF root_id IS NULL OR NOT EXISTS(SELECT 1 FROM public.gateway_command_principals
   WHERE login_name=session_user AND tenant_id=root_id AND capability=p_capability) THEN
  RAISE EXCEPTION 'gateway principal denied' USING ERRCODE='42501';
 END IF;
 IF p_capability='submit' AND NOT public.lock_business_worker_tenant(requested) THEN
  RAISE EXCEPTION 'business tenant unavailable' USING ERRCODE='42501';
 END IF;
 PERFORM set_config('app.tenant_id',root_id::text,true);
 RETURN root_id;
END $$;
ALTER FUNCTION public.authorize_gateway_command(text) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.authorize_gateway_command(text) FROM PUBLIC;

CREATE FUNCTION public.lock_gateway_command_scope(p_tenant uuid,p_account uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 PERFORM 1 FROM public.tenants WHERE id=p_tenant FOR SHARE;
 PERFORM 1 FROM public.telegram_accounts WHERE tenant_id=p_tenant AND id=p_account FOR UPDATE;
 PERFORM 1 FROM public.account_gateway_routes WHERE tenant_id=p_tenant AND account_id=p_account FOR SHARE;
 PERFORM 1 FROM public.account_gateway_leases WHERE tenant_id=p_tenant AND account_id=p_account FOR UPDATE;
END $$;
ALTER FUNCTION public.lock_gateway_command_scope(uuid,uuid) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.lock_gateway_command_scope(uuid,uuid) FROM PUBLIC;


CREATE OR REPLACE FUNCTION public.submit_gateway_account_command(
    p_account_id UUID,
    p_route_generation BIGINT,
    p_operation_key UUID,
    p_destination_peer_id BIGINT,
    p_text TEXT
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_id UUID; v_existing public.gateway_account_commands%ROWTYPE;
BEGIN
    v_tenant := public.authorize_gateway_command('submit');
    PERFORM public.lock_gateway_command_scope(v_tenant,p_account_id);
    IF v_tenant IS NULL OR p_route_generation <= 0 OR p_operation_key IS NULL
       OR p_destination_peer_id = 0 OR p_text IS NULL OR length(p_text) NOT BETWEEN 1 AND 4096 THEN
        RAISE EXCEPTION 'invalid gateway command request' USING ERRCODE = '22023';
    END IF;
    -- The submitter is a business worker: validate its root/business tenant
    -- mapping and both tenant states instead of trusting a caller-set GUC.
    -- authorize_gateway_command already validated the business mapping before
    -- switching the context to the root tenant. Never recheck a root UUID as a
    -- business UUID here.
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

ALTER FUNCTION public.submit_gateway_account_command(UUID,BIGINT,UUID,BIGINT,TEXT) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.submit_gateway_account_command(UUID,BIGINT,UUID,BIGINT,TEXT) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.submit_gateway_account_command(UUID,BIGINT,UUID,BIGINT,TEXT) TO tgs_command_submitter;

CREATE OR REPLACE FUNCTION public.claim_gateway_account_command(
    p_command_id UUID,
    p_worker_id TEXT,
    p_fencing_token BIGINT,
    p_claim_seconds INTEGER DEFAULT 15
) RETURNS SETOF public.gateway_account_commands
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    PERFORM public.lock_gateway_command_scope(v_tenant,(SELECT account_id FROM public.gateway_account_commands WHERE id=p_command_id AND tenant_id=v_tenant));
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
          AND NOT EXISTS(SELECT 1 FROM public.gateway_account_commands busy WHERE busy.tenant_id=c.tenant_id AND busy.account_id=c.account_id AND busy.status IN ('claimed','unknown','flood_wait'))
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

ALTER FUNCTION public.claim_gateway_account_command(UUID,TEXT,BIGINT,INTEGER) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.claim_gateway_account_command(UUID,TEXT,BIGINT,INTEGER) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.claim_gateway_account_command(UUID,TEXT,BIGINT,INTEGER) TO tgs_command_executor;

CREATE OR REPLACE FUNCTION public.mark_gateway_account_command_dispatch_started(
    p_command_id UUID, p_worker_id TEXT, p_fencing_token BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_count INTEGER;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    PERFORM public.lock_gateway_command_scope(v_tenant,(SELECT account_id FROM public.gateway_account_commands WHERE id=p_command_id AND tenant_id=v_tenant));
    UPDATE public.gateway_account_commands c
    SET dispatch_started_at=clock_timestamp(),updated_at=clock_timestamp()
    WHERE c.id=p_command_id AND c.tenant_id=v_tenant AND c.status='claimed'
      AND c.claim_worker_id=p_worker_id AND c.claim_fencing_token=p_fencing_token
      AND c.claim_until>clock_timestamp() AND c.dispatch_started_at IS NULL
      AND EXISTS(SELECT 1 FROM public.telegram_accounts a JOIN public.tenants t ON t.id=a.tenant_id
        WHERE a.tenant_id=c.tenant_id AND a.id=c.account_id AND a.status IN ('AUTHORIZED','ACTIVE') AND t.status='ACTIVE')
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

ALTER FUNCTION public.mark_gateway_account_command_dispatch_started(UUID,TEXT,BIGINT) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.mark_gateway_account_command_dispatch_started(UUID,TEXT,BIGINT) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.mark_gateway_account_command_dispatch_started(UUID,TEXT,BIGINT) TO tgs_command_executor;

CREATE OR REPLACE FUNCTION public.finish_gateway_account_command(
    p_command_id UUID, p_worker_id TEXT, p_fencing_token BIGINT,
    p_status TEXT, p_remote_message_id BIGINT DEFAULT NULL,
    p_error_code TEXT DEFAULT NULL, p_retry_after INTEGER DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_account UUID; v_transition TEXT; v_meta JSONB; v_count INTEGER;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    IF p_status IS NULL OR p_status NOT IN ('succeeded','rejected','flood_wait','unknown') THEN
        RAISE EXCEPTION 'invalid terminal command status' USING ERRCODE = '22023';
    END IF;
    IF p_status='succeeded' AND (p_remote_message_id IS NULL OR p_remote_message_id<=0 OR p_error_code IS NOT NULL OR p_retry_after IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid success result' USING ERRCODE = '22023';
    ELSIF p_status='flood_wait' AND (p_retry_after IS NULL OR p_retry_after<1 OR p_remote_message_id IS NOT NULL OR p_error_code IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid flood wait result' USING ERRCODE = '22023';
    ELSIF p_status IN ('rejected','unknown') AND (p_error_code IS NULL OR p_error_code !~ '^[A-Z][A-Z0-9_]{0,63}$' OR p_retry_after IS NOT NULL OR p_remote_message_id IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid error result' USING ERRCODE = '22023';
    END IF;
    PERFORM public.lock_gateway_command_scope(v_tenant,(SELECT account_id FROM public.gateway_account_commands WHERE id=p_command_id AND tenant_id=v_tenant));
    UPDATE public.gateway_account_commands c
    SET status=p_status,
        result=CASE WHEN p_status='succeeded' THEN jsonb_build_object('remote_message_id',p_remote_message_id)
                    WHEN p_status='flood_wait' THEN jsonb_build_object('retry_after',p_retry_after)
                    ELSE '{}'::jsonb END,
        error_code=p_error_code,
        available_at=CASE WHEN p_status='flood_wait'
            THEN clock_timestamp()+p_retry_after*interval '1 second'
            ELSE c.available_at END,
        completed_at=CASE WHEN p_status='flood_wait' THEN NULL ELSE clock_timestamp() END,
        updated_at=clock_timestamp(),
        claim_until=NULL
    WHERE c.id=p_command_id AND c.tenant_id=v_tenant AND c.status='claimed'
      AND c.claim_worker_id=p_worker_id AND c.claim_fencing_token=p_fencing_token
      AND c.claim_until>clock_timestamp()
      -- FloodWait is a server-confirmed non-delivery result and may occur during preflight.
      AND (p_status IN ('rejected','flood_wait') OR c.dispatch_started_at IS NOT NULL)
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

ALTER FUNCTION public.finish_gateway_account_command(UUID,TEXT,BIGINT,TEXT,BIGINT,TEXT,INTEGER) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.finish_gateway_account_command(UUID,TEXT,BIGINT,TEXT,BIGINT,TEXT,INTEGER) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.finish_gateway_account_command(UUID,TEXT,BIGINT,TEXT,BIGINT,TEXT,INTEGER) TO tgs_command_executor;

CREATE FUNCTION public.requeue_ready_gateway_account_flood_wait(
    p_account_id UUID, p_worker_id TEXT, p_fencing_token BIGINT
) RETURNS INTEGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $flood_wait$
DECLARE v_tenant UUID; v_count INTEGER := 0;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    IF v_tenant IS NULL OR p_account_id IS NULL OR p_worker_id IS NULL
       OR length(p_worker_id) NOT BETWEEN 1 AND 150 OR p_fencing_token <= 0 THEN
        RAISE EXCEPTION 'invalid flood-wait recovery request' USING ERRCODE = '22023';
    END IF;
    PERFORM public.lock_gateway_command_scope(v_tenant,p_account_id);
    IF NOT EXISTS (
        SELECT 1 FROM public.account_gateway_leases l
        WHERE l.tenant_id=v_tenant AND l.account_id=p_account_id
          AND l.worker_id=p_worker_id AND l.fencing_token=p_fencing_token
          AND l.lease_expires_at>clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'current gateway lease required' USING ERRCODE = '42501';
    END IF;
    WITH requeued AS (
        UPDATE public.gateway_account_commands c
        SET status='queued',
            available_at=clock_timestamp(),
            result=NULL,
            error_code=NULL,
            completed_at=NULL,
            dispatch_started_at=NULL,
            claim_worker_id=NULL,
            claim_fencing_token=NULL,
            claim_until=NULL,
            updated_at=clock_timestamp()
        WHERE c.tenant_id=v_tenant AND c.account_id=p_account_id
          AND c.status='flood_wait' AND c.available_at<=clock_timestamp()
        RETURNING c.id,c.tenant_id,c.account_id
    ), logged AS (
        INSERT INTO public.gateway_account_command_events(
            tenant_id,command_id,account_id,transition,actor_class,metadata
        )
        SELECT tenant_id,id,account_id,'requeued','gateway',
            jsonb_build_object('reason_code','FLOOD_WAIT_ELAPSED')
        FROM requeued
        RETURNING 1
    )
    SELECT count(*)::integer INTO v_count FROM requeued;
    RETURN COALESCE(v_count,0);
END;
$flood_wait$;

ALTER FUNCTION public.requeue_ready_gateway_account_flood_wait(UUID,TEXT,BIGINT) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.requeue_ready_gateway_account_flood_wait(UUID,TEXT,BIGINT) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.requeue_ready_gateway_account_flood_wait(UUID,TEXT,BIGINT) TO tgs_command_executor;

CREATE OR REPLACE FUNCTION public.claim_next_gateway_account_command(
    p_account_id UUID,
    p_worker_id TEXT,
    p_fencing_token BIGINT,
    p_claim_seconds INTEGER DEFAULT 15
) RETURNS SETOF public.gateway_account_commands
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    PERFORM public.lock_gateway_command_scope(v_tenant,p_account_id);
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
          AND NOT EXISTS(SELECT 1 FROM public.gateway_account_commands busy WHERE busy.tenant_id=c.tenant_id AND busy.account_id=c.account_id AND busy.status IN ('claimed','unknown','flood_wait'))
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

ALTER FUNCTION public.claim_next_gateway_account_command(UUID,TEXT,BIGINT,INTEGER) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.claim_next_gateway_account_command(UUID,TEXT,BIGINT,INTEGER) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.claim_next_gateway_account_command(UUID,TEXT,BIGINT,INTEGER) TO tgs_command_executor;

CREATE OR REPLACE FUNCTION public.recover_expired_gateway_account_commands(
    p_account_id UUID, p_worker_id TEXT, p_fencing_token BIGINT
) RETURNS TABLE(requeued_before_dispatch INTEGER, marked_unknown INTEGER)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_tenant UUID; v_requeued INTEGER := 0; v_unknown INTEGER := 0;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    PERFORM public.lock_gateway_command_scope(v_tenant,p_account_id);
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

ALTER FUNCTION public.recover_expired_gateway_account_commands(UUID,TEXT,BIGINT) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.recover_expired_gateway_account_commands(UUID,TEXT,BIGINT) FROM PUBLIC,tgs_runtime;
GRANT EXECUTE ON FUNCTION public.recover_expired_gateway_account_commands(UUID,TEXT,BIGINT) TO tgs_command_executor;

CREATE FUNCTION public.get_gateway_account_command_result(p_command_id UUID)
RETURNS TABLE(
    command_id UUID,
    account_id UUID,
    status TEXT,
    result JSONB,
    error_code TEXT,
    available_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $command_result$
DECLARE v_tenant UUID;
BEGIN
    v_tenant := public.authorize_gateway_command('submit');
    IF p_command_id IS NULL THEN
        RAISE EXCEPTION 'command id required' USING ERRCODE = '22023';
    END IF;
    -- Return no row for an unknown or cross-tenant identifier. Never expose
    -- command payload text or reveal whether another tenant owns the id.
    RETURN QUERY
        SELECT c.id,c.account_id,c.status,c.result,c.error_code,
               c.available_at,c.created_at,c.completed_at
        FROM public.gateway_account_commands c
        WHERE c.tenant_id=v_tenant AND c.id=p_command_id;
END;
$command_result$;

ALTER FUNCTION public.get_gateway_account_command_result(UUID) OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.get_gateway_account_command_result(UUID) FROM PUBLIC,tgs_runtime,tgs_command_executor;
GRANT EXECUTE ON FUNCTION public.get_gateway_account_command_result(UUID) TO tgs_command_submitter;

COMMIT;
