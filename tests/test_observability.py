from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import pytest

from app.observability import configure_request_metrics, configure_request_tracing


def test_tracing_is_disabled_without_an_explicit_otlp_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = FastAPI()

    assert configure_request_tracing(app) is None
    assert app.user_middleware == []


def test_tracing_records_route_template_and_w3c_parent_without_request_secrets():
    app = FastAPI()

    @app.get("/accounts/{account_id}")
    async def account(account_id: str):
        return {"account_id": account_id}

    exporter = InMemorySpanExporter()
    provider = configure_request_tracing(
        app,
        endpoint="http://collector:4318/v1/traces",
        service_name="tgmate-api-test",
        environment="test",
        sample_rate=1.0,
        exporter=exporter,
    )
    assert provider is not None

    try:
        with TestClient(app) as client:
            response = client.get(
                "/accounts/private-account-id?token=private-query-token",
                headers={
                    "traceparent": (
                        "00-4bf92f3577b34da6a3ce929d0e0e4736-"
                        "00f067aa0ba902b7-01"
                    )
                },
            )

        assert response.status_code == 200
        assert provider.force_flush()

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "GET /accounts/{account_id}"
        assert span.attributes["http.request.method"] == "GET"
        assert span.attributes["http.route"] == "/accounts/{account_id}"
        assert span.attributes["http.response.status_code"] == 200
        assert span.parent is not None
        assert span.parent.trace_id == int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
        assert span.parent.span_id == int("00f067aa0ba902b7", 16)
        assert span.resource.attributes["service.name"] == "tgmate-api-test"
        assert "private-account-id" not in repr(span.attributes)
        assert "private-query-token" not in repr(span.attributes)
    finally:
        provider.shutdown()


def test_tracing_marks_server_errors_without_recording_exception_messages():
    app = FastAPI()

    @app.get("/failure")
    async def failure():
        raise RuntimeError("private user data must not enter telemetry")

    exporter = InMemorySpanExporter()
    provider = configure_request_tracing(
        app,
        endpoint="http://collector:4318/v1/traces",
        sample_rate=1.0,
        exporter=exporter,
    )
    assert provider is not None

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/failure")

        assert response.status_code == 500
        assert provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["exception.type"] == "RuntimeError"
        assert "private user data" not in repr(span.attributes)
    finally:
        provider.shutdown()


def test_tracing_rejects_invalid_sampling_rate():
    app = FastAPI()
    with pytest.raises(RuntimeError, match="OTEL_TRACES_SAMPLER_ARG"):
        configure_request_tracing(
            app,
            endpoint="http://collector:4318/v1/traces",
            sample_rate=1.1,
            exporter=InMemorySpanExporter(),
        )



def test_metrics_are_disabled_without_an_explicit_otlp_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = FastAPI()

    assert configure_request_metrics(app) is None
    assert app.user_middleware == []


def test_metrics_record_bounded_route_labels_without_request_secrets():
    app = FastAPI()

    @app.get("/accounts/{account_id}")
    async def account(account_id: str):
        return {"account_id": account_id}

    reader = InMemoryMetricReader()
    provider = configure_request_metrics(
        app,
        endpoint="http://collector:4318/v1/metrics",
        service_name="tgmate-api-test",
        environment="test",
        reader=reader,
    )
    assert provider is not None

    try:
        with TestClient(app) as client:
            response = client.get(
                "/accounts/private-account-id?token=private-query-token",
                headers={"x-private-header": "must-not-be-recorded"},
            )

        assert response.status_code == 200
        data = reader.get_metrics_data()
        assert data is not None
        metrics_by_name = {
            metric.name: metric
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert set(metrics_by_name) == {
            "http.server.request.count",
            "http.server.request.duration",
        }
        count_points = metrics_by_name["http.server.request.count"].data.data_points
        duration_points = metrics_by_name["http.server.request.duration"].data.data_points
        assert len(count_points) == 1
        assert len(duration_points) == 1
        attributes = dict(count_points[0].attributes)
        assert attributes == {
            "http.request.method": "GET",
            "http.route": "/accounts/{account_id}",
            "http.response.status_code": 200,
        }
        assert count_points[0].value == 1
        assert duration_points[0].count == 1
        assert "private-account-id" not in repr(attributes)
        assert "private-query-token" not in repr(attributes)
        assert "must-not-be-recorded" not in repr(attributes)
    finally:
        provider.shutdown()


def test_metrics_exclude_health_probes_and_bound_unknown_methods():
    app = FastAPI()

    @app.get("/health/ready")
    async def ready():
        return {"ready": True}

    @app.api_route("/metrics-test", methods=["GET", "TRACE"])
    async def metrics_test():
        return {"ok": True}

    reader = InMemoryMetricReader()
    provider = configure_request_metrics(
        app,
        endpoint="http://collector:4318/v1/metrics",
        reader=reader,
    )
    assert provider is not None

    try:
        with TestClient(app) as client:
            assert client.get("/health/ready").status_code == 200
            assert client.request("TRACE", "/metrics-test").status_code == 200

        data = reader.get_metrics_data()
        metrics_by_name = {
            metric.name: metric
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        points = metrics_by_name["http.server.request.count"].data.data_points
        assert len(points) == 1
        attributes = dict(points[0].attributes)
        assert attributes["http.request.method"] == "OTHER"
        assert attributes["http.route"] == "/metrics-test"
    finally:
        provider.shutdown()
