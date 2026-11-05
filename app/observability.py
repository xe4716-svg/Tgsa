"""Opt-in, privacy-conscious OpenTelemetry request tracing for the API.

Tracing is disabled unless an OTLP endpoint is configured. The middleware records
normalized route templates and status codes only; it never records request bodies,
query strings, headers, peer identifiers, or exception messages.
"""
from __future__ import annotations

import os
import time
from typing import Any

from opentelemetry import propagate
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer


class RequestTraceMiddleware:
    """Create one server span per non-probe HTTP request without capturing secrets."""

    def __init__(self, app: Any, tracer: Tracer) -> None:
        self.app = app
        self.tracer = tracer

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Liveness/readiness probes are high-volume and already have dedicated health
        # checks. Excluding them keeps operational traces focused on user requests.
        if scope.get("path") in {"/health/live", "/health/ready"}:
            await self.app(scope, receive, send)
            return

        # Only accept W3C trace context. Baggage and all other request headers are
        # intentionally excluded from the telemetry carrier.
        carrier = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() in {b"traceparent", b"tracestate"}
        }
        parent_context = propagate.extract(carrier)
        method = str(scope.get("method", "HTTP"))

        with self.tracer.start_as_current_span(
            f"HTTP {method}",
            context=parent_context,
            kind=SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.request.method", method)

            async def send_with_status(message: dict[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    status_code = message.get("status")
                    if isinstance(status_code, int):
                        span.set_attribute("http.response.status_code", status_code)
                        if status_code >= 500:
                            span.set_status(Status(StatusCode.ERROR, "server_error"))

                    # Starlette attaches the matched route to the shared scope while
                    # dispatching. Use its template, never the raw path (which may
                    # contain account IDs, invite tokens, or other identifiers).
                    route_path = getattr(scope.get("route"), "path", None)
                    if isinstance(route_path, str):
                        span.set_attribute("http.route", route_path)
                        span.update_name(f"{method} {route_path}")
                await send(message)

            try:
                await self.app(scope, receive, send_with_status)
            except Exception as exc:
                # Do not record exception messages or stack traces: they can contain
                # credentials or user-supplied values. The type is enough to classify.
                span.set_attribute("exception.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, "request_exception"))
                raise


def configure_request_tracing(
    app: Any,
    *,
    endpoint: str | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    sample_rate: float | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Enable OTLP tracing only when an endpoint is explicitly configured.

    When endpoint is omitted, the standard OTEL_EXPORTER_OTLP_TRACES_ENDPOINT or
    OTEL_EXPORTER_OTLP_ENDPOINT environment variables are used by the exporter.
    The optional exporter argument exists for deterministic in-memory tests.
    """
    configured_endpoint = endpoint
    if configured_endpoint is None:
        configured_endpoint = (
            os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
    if not configured_endpoint or not configured_endpoint.strip():
        return None

    configured_rate = sample_rate
    if configured_rate is None:
        try:
            configured_rate = float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "0.1"))
        except ValueError as exc:
            raise RuntimeError("OTEL_TRACES_SAMPLER_ARG must be a number from 0 to 1") from exc
    if not 0.0 <= configured_rate <= 1.0:
        raise RuntimeError("OTEL_TRACES_SAMPLER_ARG must be a number from 0 to 1")

    if exporter is None:
        # Let the OTLP exporter apply the standard endpoint/header/timeout env vars
        # when deployment configuration supplies the endpoint.
        exporter = (
            OTLPSpanExporter(endpoint=configured_endpoint)
            if endpoint is not None
            else OTLPSpanExporter()
        )

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "tgmate-api"),
                "deployment.environment.name": environment or os.getenv("APP_ENV", "development"),
            }
        ),
        sampler=ParentBased(root=TraceIdRatioBased(configured_rate)),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    app.add_middleware(
        RequestTraceMiddleware,
        tracer=provider.get_tracer("tgmate.http", "1.0.0"),
    )
    app.state.tracing_provider = provider
    return provider


class RequestMetricsMiddleware:
    """Record low-cardinality HTTP request counts and durations without secrets."""

    _METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

    def __init__(self, app: Any, request_counter: Any, request_duration: Any) -> None:
        self.app = app
        self.request_counter = request_counter
        self.request_duration = request_duration

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in {"/health/live", "/health/ready"}:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method_value = str(scope.get("method", "OTHER")).upper()
        method = method_value if method_value in self._METHODS else "OTHER"
        response_status = 500

        async def send_with_metrics(message: dict[str, Any]) -> None:
            nonlocal response_status
            if message.get("type") == "http.response.start":
                status = message.get("status")
                if isinstance(status, int) and 100 <= status <= 599:
                    response_status = status
            await send(message)

        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            # Use only a matched route template. Unmatched requests share one
            # bounded label and raw paths/query strings are never metric attributes.
            route_path = getattr(scope.get("route"), "path", None)
            route = route_path if isinstance(route_path, str) else "_unmatched"
            attributes = {
                "http.request.method": method,
                "http.route": route,
                "http.response.status_code": response_status,
            }
            self.request_counter.add(1, attributes)
            self.request_duration.record(time.perf_counter() - started, attributes)


def configure_request_metrics(
    app: Any,
    *,
    endpoint: str | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    exporter: MetricExporter | None = None,
    reader: MetricReader | None = None,
) -> MeterProvider | None:
    """Enable OTLP HTTP request metrics only when an endpoint is configured.

    Labels are bounded to method, matched route template, and response status.
    The optional exporter/reader arguments support deterministic unit tests.
    """
    configured_endpoint = endpoint
    if configured_endpoint is None:
        configured_endpoint = (
            os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
    if not configured_endpoint or not configured_endpoint.strip():
        return None

    if reader is None:
        if exporter is None:
            exporter = (
                OTLPMetricExporter(endpoint=configured_endpoint)
                if endpoint is not None
                else OTLPMetricExporter()
            )
        reader = PeriodicExportingMetricReader(exporter)

    provider = MeterProvider(
        resource=Resource.create(
            {
                "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "tgmate-api"),
                "deployment.environment.name": environment or os.getenv("APP_ENV", "development"),
            }
        ),
        metric_readers=[reader],
    )
    meter = provider.get_meter("tgmate.http", "1.0.0")
    request_counter = meter.create_counter(
        "http.server.request.count",
        description="Number of completed HTTP server requests.",
        unit="1",
    )
    request_duration = meter.create_histogram(
        "http.server.request.duration",
        description="Duration of completed HTTP server requests.",
        unit="s",
    )
    app.add_middleware(
        RequestMetricsMiddleware,
        request_counter=request_counter,
        request_duration=request_duration,
    )
    app.state.metrics_provider = provider
    return provider
