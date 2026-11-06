-- Persist only the W3C parent needed to correlate a route activation with its
-- API request. No baggage, tracestate, tenant identifiers or request metadata.
ALTER TABLE account_gateway_routes
    ADD COLUMN traceparent VARCHAR(55);

ALTER TABLE account_gateway_routes
    ADD CONSTRAINT account_gateway_routes_traceparent_format
    CHECK (
        traceparent IS NULL
        OR traceparent ~ '^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'
    );
