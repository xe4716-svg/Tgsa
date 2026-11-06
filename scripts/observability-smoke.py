#!/usr/bin/env python3
"""Validate authenticated OTLP ingestion into the isolated local stack."""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local" / "observability"
ENV_FILE = LOCAL / ".env"
CA_FILE = LOCAL / "tls" / "ca.crt"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def request_json(url: str, *, context=None, timeout=3):
    with urllib.request.urlopen(url, context=context, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until_ready() -> None:
    checks = {
        "Collector": ("http://127.0.0.1:13133/", None),
        "Tempo": ("http://127.0.0.1:3200/ready", None),
        "Prometheus": ("http://127.0.0.1:9090/-/ready", None),
        "Grafana": ("http://127.0.0.1:3000/api/health", None),
    }
    deadline = time.monotonic() + 90
    pending = set(checks)
    while pending and time.monotonic() < deadline:
        for name in tuple(pending):
            url, context = checks[name]
            try:
                with urllib.request.urlopen(url, context=context, timeout=2) as response:
                    if response.status == 200:
                        pending.remove(name)
            except Exception:
                pass
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeError("Local observability endpoints not ready: " + ", ".join(sorted(pending)))


def verify_authentication(token: str, tls_context: ssl.SSLContext) -> None:
    request = urllib.request.Request(
        "https://localhost:4318/v1/traces",
        data=b"",
        headers={"Content-Type": "application/x-protobuf"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, context=tls_context, timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise RuntimeError(f"Unauthenticated OTLP request returned HTTP {exc.code}, expected 401") from exc
    else:
        raise RuntimeError("Collector accepted an unauthenticated OTLP request")

    authenticated = urllib.request.Request(
        "https://localhost:4318/v1/traces",
        data=b"",
        headers={
            "Content-Type": "application/x-protobuf",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(authenticated, context=tls_context, timeout=5)
    except urllib.error.HTTPError as exc:
        # Empty protobuf is expected to be rejected after authentication.
        if exc.code in (401, 403):
            raise RuntimeError("Collector rejected the configured bearer token") from exc


def send_smoke_telemetry(token: str, run_id: str) -> str:
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://localhost:4318"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {token}"
    os.environ["OTEL_EXPORTER_OTLP_CERTIFICATE"] = str(CA_FILE)
    os.environ["OTEL_SERVICE_NAME"] = "tgmate-observability-smoke"

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "tgmate-observability-smoke"}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    tracer = provider.get_tracer("tgmate.observability.smoke", "1.0.0")
    with tracer.start_as_current_span("tgmate.observability.smoke") as span:
        span.set_attribute("smoke.run_id", run_id)
    trace_id = f"{span.get_span_context().trace_id:032x}"
    if not provider.force_flush(timeout_millis=10000):
        raise RuntimeError("Trace exporter did not flush")
    provider.shutdown()

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5000)
    metrics = MeterProvider(
        resource=Resource.create({"service.name": "tgmate-observability-smoke"}),
        metric_readers=[reader],
    )
    counter = metrics.get_meter("tgmate.observability.smoke", "1.0.0").create_counter(
        "tgmate.observability.smoke"
    )
    counter.add(1, {"smoke": "true"})
    if not metrics.force_flush(timeout_millis=10000):
        raise RuntimeError("Metrics exporter did not flush")
    metrics.shutdown()
    return trace_id


def verify_trace(trace_id: str) -> None:
    query = urllib.parse.urlencode({"tags": "service.name=tgmate-observability-smoke", "limit": "20"})
    url = f"http://127.0.0.1:3200/api/search?{query}"
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        try:
            result = request_json(url)
            traces = result.get("traces", [])
            if any(item.get("traceID") == trace_id for item in traces):
                trace_data = request_json(f"http://127.0.0.1:3200/api/v2/traces/{trace_id}")
                if "tgmate.observability.smoke" not in json.dumps(trace_data):
                    raise RuntimeError("Tempo found the trace but the expected smoke span was absent")
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("Tempo did not return the OTLP smoke trace within 75 seconds")


def verify_metrics() -> None:
    query = urllib.parse.urlencode({"query": "tgmate_observability_smoke_total"})
    url = f"http://127.0.0.1:9090/api/v1/query?{query}"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            result = request_json(url)
            if result.get("status") == "success" and result.get("data", {}).get("result"):
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("Prometheus did not scrape the OTLP smoke counter within 45 seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-only", action="store_true")
    args = parser.parse_args()

    if not ENV_FILE.is_file() or not CA_FILE.is_file():
        raise RuntimeError("Run scripts/observability-setup.sh before the observability smoke test")
    wait_until_ready()
    if args.wait_only:
        print("Collector, Tempo, Prometheus and Grafana are ready.")
        return 0

    env = load_env()
    token = env["OTEL_COLLECTOR_BEARER_TOKEN"]
    tls_context = ssl.create_default_context(cafile=str(CA_FILE))
    verify_authentication(token, tls_context)
    run_id = str(uuid4())
    trace_id = send_smoke_telemetry(token, run_id)
    verify_trace(trace_id)
    verify_metrics()
    print("Authenticated OTLP trace + metrics export, Tempo query and Prometheus scrape passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Observability smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
