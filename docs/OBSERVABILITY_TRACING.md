# TGMate API tracing (opt-in PoC)

## Scope

This change adds the first, API-only OpenTelemetry tracing slice on top of the
TGMate root runtime. It is not a complete observability deployment:

- Incoming W3C trace context is extracted from `traceparent` / `tracestate`.
- One server span is emitted for each non-health HTTP request.
- Span names and `http.route` use the FastAPI route template, not the raw URL.
- Only method, normalized route, response status, service name, and environment
  are recorded.
- Request bodies, query strings, headers, account/tenant/peer identifiers, and
  exception messages are deliberately not recorded.
- Liveness and readiness probes are excluded.
- No telemetry is emitted unless an OTLP endpoint is explicitly configured.

An independently opt-in HTTP metrics slice is also available:
- `http.server.request.count` counter and `http.server.request.duration` histogram.
- Labels are limited to a normalized method, FastAPI route template (or one fixed
  `_unmatched` value), and numeric response status.
- Health probes are excluded; unknown HTTP methods are collapsed to `OTHER`.
- Raw paths, query strings, headers, tenant/account/peer identifiers and exception
  messages are never metric labels.
- Metrics are disabled unless `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` or the common
  `OTEL_EXPORTER_OTLP_ENDPOINT` is configured.

Example metrics endpoint:
```dotenv
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://otel-collector:4318/v1/metrics
```

Database auto-instrumentation remains intentionally disabled. SQL statements,
bind values, and Redis keys can contain sensitive data; add database spans only
after a separate attribute-redaction review. API metrics remain API-only; worker
queue depth, Gateway command outcomes, business-operation metrics, and
trace-to-log correlation are not yet instrumented.

## Configuration

Set one of the standard OpenTelemetry OTLP endpoint variables in the API
runtime environment:

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=tgmate-api
OTEL_TRACES_SAMPLER_ARG=0.1
```

Or set the trace-specific endpoint:

```dotenv
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
```

The HTTP exporter uses the standard OpenTelemetry environment configuration for
headers, timeout, and compression. Use HTTPS or a trusted private network for
export. Do not put collector credentials in source control.

- If neither endpoint is set, tracing is disabled and the API makes no telemetry
  network calls.
- The sampling ratio must be between `0` and `1`; default is `0.1`.
- The default service name is `tgmate-api`; deployments may override it with
  `OTEL_SERVICE_NAME`.
- `APP_ENV` supplies the deployment environment resource attribute.

## Local validation

The unit tests use OpenTelemetry's in-memory span exporter. They verify opt-in
behavior, W3C parent propagation, normalized route naming, health-probe
exclusion, server-error classification, and absence of request/exception
secrets from span attributes. The metrics tests verify opt-in behavior, route-template
labels, health-probe exclusion, bounded method labels, and absence of request secrets.

These tests do not prove that a real Collector or backend is deployed. A
deployment PoC still needs a Collector endpoint, retention/authentication
configuration, trace-to-log correlation, and an operational restore/incident
drill.

## Source inspiration

The implementation uses the OpenTelemetry API/SDK and OTLP exporter directly.
The MIT-licensed `blueswen/fastapi-observability` repository was reviewed as a
reference for Tempo/Prometheus/Loki correlation and Compose-based demos; its
Loki Docker logging-driver plugin and demo credentials are intentionally not
adopted as TGMate runtime dependencies.


## Durable API-to-Gateway route activation trace

The desired Gateway route is a durable control-plane record, so the route-change
API stores only the active server span's W3C `traceparent` in
`account_gateway_routes.traceparent`. It does not persist `tracestate`,
baggage, raw request headers, or tenant/account identifiers.

- The route schema validates the version-00 W3C traceparent format.
- The supervisor reads and clears the stored context under a row lock, so only
  one supervisor poll can consume it.
- Trace metadata is excluded from route equality and public route responses;
  clearing it never restarts a healthy account connection.
- The Gateway worker extracts the remote parent and emits a short
  `gateway.account.activate` consumer span. The span ends once the account is
  authorized and ready; it does not remain open for the potentially long-lived
  MTProto socket.
- Attributes are limited to numeric route generation, bounded outcome code,
  and (on startup failure only) exception class plus the fixed source function
  name. Account IDs, tenant IDs, session material, proxy settings, exception
  messages, and Telegram user identifiers are never span attributes.
- Worker tracing is independently opt-in through
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`.
  The worker owns and shuts down its provider. Without an endpoint it emits no
  telemetry and the Gateway runs unchanged.

This establishes API request → durable desired-route update → Gateway activation
correlation only. It does not claim propagation into a business command queue:
the currently integrated Gateway runtime supervises read-only account
connections and does not yet execute the persistent send-command queue tracked
separately in PR #28.

Worker configuration can use the same private OTLP endpoint and sampler settings:

```dotenv
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
OTEL_SERVICE_NAME=tgmate-mtproto-gateway
OTEL_TRACES_SAMPLER_ARG=0.1
```
