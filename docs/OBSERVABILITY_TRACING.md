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

The first slice intentionally does not auto-instrument asyncpg or Redis. SQL
statements, bind values, and Redis keys can contain sensitive data; add database
spans only after a separate attribute-redaction review. Worker/Gateway trace propagation and log correlation remain follow-up work. Metrics are API-only; worker queue depth, Gateway command outcomes and business-operation metrics are not yet instrumented.

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
