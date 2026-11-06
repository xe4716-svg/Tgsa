"""Low-cardinality OpenTelemetry metrics for the durable Gateway queue."""
from __future__ import annotations

import os
import threading
from collections.abc import Iterable

from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource

_SAFE_COMMAND_KINDS = {"preflight_send_text"}
_SAFE_OUTCOMES = {"SUCCEEDED", "FLOOD_WAIT", "UNKNOWN", "ERROR"}
_QUEUE_STATES = ("queued", "claimed", "unknown", "flood_wait")


class GatewayCommandMetrics:
    """Command metrics with bounded labels and in-memory aggregated queue gauges."""

    def __init__(self, meter: Meter):
        self._executions = meter.create_counter(
            "tgmate.gateway.command.executions",
            unit="{command}",
            description="Gateway command execution outcomes",
        )
        self._duration = meter.create_histogram(
            "tgmate.gateway.command.duration",
            unit="s",
            description="Time spent executing a claimed Gateway command",
        )
        self._queue_age = meter.create_histogram(
            "tgmate.gateway.command.queue.age",
            unit="s",
            description="Age of a command when claimed by the Gateway",
        )
        self._flood_wait = meter.create_histogram(
            "tgmate.gateway.command.flood_wait",
            unit="s",
            description="Telegram-provided FloodWait duration",
        )
        self._unknown = meter.create_counter(
            "tgmate.gateway.command.unknown",
            unit="{command}",
            description="Commands whose remote result requires reconciliation",
        )
        self._lock = threading.Lock()
        self._snapshots: dict[str, dict[str, float]] = {}
        meter.create_observable_gauge(
            "tgmate.gateway.command.queue.depth",
            callbacks=[self._observe_queue_depth],
            unit="{command}",
            description="Aggregated durable command queue depth by bounded state",
        )
        meter.create_observable_gauge(
            "tgmate.gateway.command.queue.oldest_age",
            callbacks=[self._observe_oldest_age],
            unit="s",
            description="Oldest queued command age across this Gateway worker",
        )

    @staticmethod
    def _kind(value: object) -> str:
        return value if isinstance(value, str) and value in _SAFE_COMMAND_KINDS else "OTHER"

    @staticmethod
    def _outcome(value: object) -> str:
        return value if isinstance(value, str) and value in _SAFE_OUTCOMES else "ERROR"

    def record_execution(
        self,
        command_kind: object,
        outcome: object,
        duration_seconds: float,
        queue_age_seconds: float | None = None,
    ) -> None:
        kind = self._kind(command_kind)
        result = self._outcome(outcome)
        attributes = {"command_kind": kind, "outcome": result}
        self._executions.add(1, attributes)
        self._duration.record(max(0.0, float(duration_seconds)), attributes)
        if queue_age_seconds is not None:
            self._queue_age.record(max(0.0, float(queue_age_seconds)), {"command_kind": kind})
        if result == "UNKNOWN":
            self._unknown.add(1, {"command_kind": kind})

    def record_flood_wait(self, seconds: int) -> None:
        if type(seconds) is int and seconds > 0:
            self._flood_wait.record(float(seconds))

    def set_queue_snapshot(self, account_key: str, snapshot: dict[str, object]) -> None:
        # account_key is used only in process memory; it is never a metric label.
        values = {
            "queued": max(0.0, float(snapshot.get("queued_count", 0) or 0)),
            "claimed": max(0.0, float(snapshot.get("claimed_count", 0) or 0)),
            "unknown": max(0.0, float(snapshot.get("unknown_count", 0) or 0)),
            "flood_wait": max(0.0, float(snapshot.get("flood_wait_count", 0) or 0)),
            "oldest_age": max(0.0, float(snapshot.get("oldest_queued_age_seconds", 0) or 0)),
        }
        with self._lock:
            self._snapshots[account_key] = values

    def clear_queue_snapshot(self, account_key: str) -> None:
        with self._lock:
            self._snapshots.pop(account_key, None)

    def _copy_snapshots(self) -> list[dict[str, float]]:
        with self._lock:
            return [dict(value) for value in self._snapshots.values()]

    def _observe_queue_depth(self, options: CallbackOptions) -> Iterable[Observation]:
        snapshots = self._copy_snapshots()
        return [
            Observation(sum(item[state] for item in snapshots), {"state": state})
            for state in _QUEUE_STATES
        ]

    def _observe_oldest_age(self, options: CallbackOptions) -> Iterable[Observation]:
        snapshots = self._copy_snapshots()
        return [Observation(max((item["oldest_age"] for item in snapshots), default=0.0))]


def configure_worker_metrics(
    *,
    endpoint: str | None = None,
    service_name: str | None = None,
    environment: str | None = None,
) -> MeterProvider | None:
    """Create an opt-in OTLP metrics provider; the caller owns shutdown."""
    configured_endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not configured_endpoint or not configured_endpoint.strip():
        return None
    exporter = (
        OTLPMetricExporter(endpoint=configured_endpoint)
        if endpoint is not None
        else OTLPMetricExporter()
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000)
    provider = MeterProvider(
        resource=Resource.create(
            {
                "service.name": service_name or os.getenv("OTEL_SERVICE_NAME", "tgmate-gateway-worker"),
                "deployment.environment.name": environment or os.getenv("APP_ENV", "development"),
            }
        ),
        metric_readers=[reader],
    )
    return provider
