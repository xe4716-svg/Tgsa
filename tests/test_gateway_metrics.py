from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.gateway_metrics import GatewayCommandMetrics, configure_worker_metrics


def _metrics(reader):
    data=reader.get_metrics_data()
    return {
        metric.name:metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_gateway_command_metrics_use_bounded_labels_and_aggregate_queue_gauges():
    reader=InMemoryMetricReader()
    provider=MeterProvider(metric_readers=[reader])
    metrics=GatewayCommandMetrics(provider.get_meter("gateway-metrics-test"))
    try:
        metrics.record_execution("preflight_send_text","SUCCEEDED",0.25,3.5)
        metrics.record_execution("unbounded-user-operation","UNKNOWN",0.5,4.0)
        metrics.record_flood_wait(17)
        metrics.set_queue_snapshot(
            "private-account-a",
            {
                "queued_count":2,
                "claimed_count":1,
                "unknown_count":1,
                "flood_wait_count":3,
                "oldest_queued_age_seconds":12.5,
            },
        )
        metrics.set_queue_snapshot(
            "private-account-b",
            {
                "queued_count":4,
                "claimed_count":0,
                "unknown_count":0,
                "flood_wait_count":0,
                "oldest_queued_age_seconds":5.0,
            },
        )
        values=_metrics(reader)

        executions=values["tgmate.gateway.command.executions"].data.data_points
        assert sum(point.value for point in executions)==2
        assert {point.attributes["command_kind"] for point in executions}=={"preflight_send_text","OTHER"}
        assert {point.attributes["outcome"] for point in executions}=={"SUCCEEDED","UNKNOWN"}

        queue_points=values["tgmate.gateway.command.queue.depth"].data.data_points
        queue_values={point.attributes["state"]:point.value for point in queue_points}
        assert queue_values=={"queued":6,"claimed":1,"unknown":1,"flood_wait":3}
        oldest=values["tgmate.gateway.command.queue.oldest_age"].data.data_points
        assert len(oldest)==1 and oldest[0].value==12.5

        for metric in values.values():
            for point in metric.data.data_points:
                assert "account_id" not in point.attributes
                assert "tenant_id" not in point.attributes
                assert "peer_id" not in point.attributes
                assert "operation_key" not in point.attributes
    finally:
        provider.shutdown()


def test_worker_metrics_are_disabled_without_an_explicit_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT",raising=False)
    assert configure_worker_metrics() is None
