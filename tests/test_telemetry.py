from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.telemetry import (
    configure_worker_tracing,
    current_traceparent,
    extract_trace_context,
    is_valid_traceparent,
)


def test_traceparent_is_only_emitted_from_a_valid_active_span():
    provider=TracerProvider()
    try:
        assert current_traceparent() is None
        with provider.get_tracer("telemetry-test").start_as_current_span("producer"):
            value=current_traceparent()
            assert is_valid_traceparent(value)
            extracted=extract_trace_context(value)
            assert extracted is not None
        assert extract_trace_context("00-not-a-valid-traceparent") is None
        assert extract_trace_context(None) is None
    finally:
        provider.shutdown()


def test_worker_tracing_is_opt_in_and_uses_the_supplied_exporter(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT",raising=False)
    assert configure_worker_tracing() is None

    exporter=InMemorySpanExporter()
    provider=configure_worker_tracing(
        endpoint="http://collector.invalid:4318/v1/traces",
        service_name="tgmate-gateway-test",
        environment="test",
        sample_rate=1.0,
        exporter=exporter,
    )
    assert provider is not None
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("telemetry-test").start_as_current_span("worker.test"):
            assert is_valid_traceparent(current_traceparent())
        spans=exporter.get_finished_spans()
        assert any(span.name=="worker.test" for span in spans)
        assert all(span.resource.attributes["service.name"]=="tgmate-gateway-test" for span in spans)
    finally:
        provider.shutdown()


def test_worker_sampler_rejects_invalid_rate(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT","http://collector.invalid:4318/v1/traces")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG","2")
    try:
        configure_worker_tracing()
    except RuntimeError as exc:
        assert "0 and 1" in str(exc)
    else:
        raise AssertionError("invalid sample rate was accepted")
