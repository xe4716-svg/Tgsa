BEGIN;

-- Store only the W3C parent needed to join a durable command to its producer.
-- Never persist baggage, tracestate, payload-derived values or request headers.
ALTER TABLE public.gateway_account_commands
    ADD COLUMN traceparent VARCHAR(55);

ALTER TABLE public.gateway_account_commands
    ADD CONSTRAINT gateway_account_commands_traceparent_format
    CHECK (
        traceparent IS NULL
        OR traceparent ~ '^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'
    );

-- Keep the original five-argument transition function as an internal primitive.
-- This wrapper validates the carrier, delegates the existing authorization,
-- idempotency and route checks, then attaches the first producer context only.
CREATE FUNCTION public.submit_gateway_account_command_with_trace(
    p_account_id UUID,
    p_route_generation BIGINT,
    p_operation_key UUID,
    p_destination_peer_id BIGINT,
    p_text TEXT,
    p_traceparent TEXT
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_id UUID;
    v_tenant UUID;
BEGIN
    IF p_traceparent IS NOT NULL
       AND p_traceparent !~ '^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$' THEN
        RAISE EXCEPTION 'invalid trace context' USING ERRCODE = '22023';
    END IF;

    v_id := public.submit_gateway_account_command(
        p_account_id, p_route_generation, p_operation_key,
        p_destination_peer_id, p_text
    );
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;

    UPDATE public.gateway_account_commands
       SET traceparent = COALESCE(traceparent, p_traceparent)
     WHERE id = v_id AND tenant_id = v_tenant;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'gateway command trace attachment failed' USING ERRCODE = '42501';
    END IF;
    RETURN v_id;
END;
$$;

ALTER FUNCTION public.submit_gateway_account_command_with_trace(
    UUID, BIGINT, UUID, BIGINT, TEXT, TEXT
) OWNER TO tgs_command_owner;

REVOKE ALL ON FUNCTION public.submit_gateway_account_command_with_trace(
    UUID, BIGINT, UUID, BIGINT, TEXT, TEXT
) FROM PUBLIC, tgs_runtime;
GRANT EXECUTE ON FUNCTION public.submit_gateway_account_command_with_trace(
    UUID, BIGINT, UUID, BIGINT, TEXT, TEXT
) TO tgs_command_submitter;

-- Prevent submitter code from bypassing the trace-aware wrapper. The wrapper
-- calls the original function as tgs_command_owner (its SECURITY DEFINER role).
REVOKE ALL ON FUNCTION public.submit_gateway_account_command(
    UUID, BIGINT, UUID, BIGINT, TEXT
) FROM PUBLIC, tgs_runtime, tgs_command_submitter;

COMMIT;
