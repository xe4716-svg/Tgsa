"""Minimal opt-in worker tracing and W3C context helpers.

Only traceparent is persisted across durable queue boundaries. Baggage,
tracestate, payloads, tenant/account identifiers and request metadata are
intentionally excluded.
"""
from __future__ import annotations

import os
import re

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import CarrierT
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def is_valid_traceparent(value: object) -> bool:
    return isinstance(value, str) and _TRACEPARENT_RE.fullmatch(value) is not None


def current_traceparent() -> str | None:
    """Inject only the active W3C traceparent; never baggage or tracestate."""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    value = carrier.get("traceparent")
    return value if is_valid_traceparent(value) else None


def extract_trace_context(traceparent: str | None):
    """Extract a validated W3C parent, rejecting all other carrier fields."""
    if not is_valid_traceparent(traceparent):
        return None
    return TraceContextTextMapPropagator().extract({"traceparent": traceparent})


def configure_worker_tracing(
    *,
    endpoint: str | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    sample_rate: float | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Create an opt-in provider for a standalone worker process.

    The caller owns provider shutdown. No global provider or API middleware is
    changed. Without an explicitly configured endpoint this returns None.
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
        exporter = (
            OTLPSpanExporter(endpoint=configured_endpoint)
            if endpoint is not None
            else OTLPSpanExporter()
        )

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "tgmate-worker"),
                "deployment.environment.name": environment or os.getenv("APP_ENV", "development"),
            }
        ),
        sampler=ParentBased(root=TraceIdRatioBased(configured_rate)),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider
