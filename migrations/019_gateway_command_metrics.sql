BEGIN;

-- Aggregate queue health only for the account/route currently fenced by this
-- executor. No tenant/account identifiers are returned as metric dimensions.
CREATE FUNCTION public.read_gateway_account_command_metrics(
    p_account_id UUID,
    p_route_generation BIGINT,
    p_worker_id TEXT,
    p_fencing_token BIGINT
) RETURNS TABLE (
    queued_count BIGINT,
    claimed_count BIGINT,
    unknown_count BIGINT,
    flood_wait_count BIGINT,
    oldest_queued_age_seconds DOUBLE PRECISION
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_tenant UUID;
BEGIN
    v_tenant := public.authorize_gateway_command('execute');
    IF p_account_id IS NULL OR p_route_generation <= 0
       OR p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 150
       OR p_fencing_token <= 0 THEN
        RAISE EXCEPTION 'invalid Gateway metrics scope' USING ERRCODE = '22023';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.account_gateway_routes r
          JOIN public.account_gateway_leases l
            ON l.tenant_id = r.tenant_id AND l.account_id = r.account_id
         WHERE r.tenant_id = v_tenant
           AND r.account_id = p_account_id
           AND r.enabled
           AND r.generation = p_route_generation
           AND l.worker_id = p_worker_id
           AND l.fencing_token = p_fencing_token
           AND l.lease_expires_at > clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'Gateway metrics require current route lease' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT
        count(*) FILTER (WHERE c.status = 'queued'),
        count(*) FILTER (WHERE c.status = 'claimed'),
        count(*) FILTER (WHERE c.status = 'unknown'),
        count(*) FILTER (WHERE c.status = 'flood_wait'),
        COALESCE(
            EXTRACT(EPOCH FROM (
                clock_timestamp() - min(c.created_at) FILTER (WHERE c.status = 'queued')
            )),
            0
        )::DOUBLE PRECISION
    FROM public.gateway_account_commands c
    WHERE c.tenant_id = v_tenant AND c.account_id = p_account_id;
END;
$$;

ALTER FUNCTION public.read_gateway_account_command_metrics(UUID, BIGINT, TEXT, BIGINT)
    OWNER TO tgs_command_owner;
REVOKE ALL ON FUNCTION public.read_gateway_account_command_metrics(UUID, BIGINT, TEXT, BIGINT)
    FROM PUBLIC, tgs_runtime;
GRANT EXECUTE ON FUNCTION public.read_gateway_account_command_metrics(UUID, BIGINT, TEXT, BIGINT)
    TO tgs_command_executor;

COMMIT;
