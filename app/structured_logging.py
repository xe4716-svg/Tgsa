"""Allowlisted JSON logs correlated with the active OpenTelemetry span.

The formatter deliberately ignores LogRecord.message, args, exception text and
arbitrary extra fields. Only explicitly allowlisted event metadata is emitted.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

from opentelemetry import trace


_SAFE_EVENTS = {
    "gateway.command.completed",
    "gateway.command.flood_wait",
    "gateway.command.unknown",
    "gateway.command.error",
}
_SAFE_OUTCOMES = {"SUCCEEDED", "FLOOD_WAIT", "UNKNOWN", "ERROR"}
_SAFE_COMMAND_KINDS = {"preflight_send_text"}


class TraceContextFilter(logging.Filter):
    """Attach only validated trace/span IDs from the active span."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            record.trace_id = f"{context.trace_id:032x}"
            record.span_id = f"{context.span_id:016x}"
        else:
            record.trace_id = None
            record.span_id = None
        return True


class SafeJsonFormatter(logging.Formatter):
    """Serialize a strict event schema; never serialize the free-form message."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event_name", None)
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": event if isinstance(event, str) and event in _SAFE_EVENTS else "application.log",
        }

        trace_id = getattr(record, "trace_id", None)
        span_id = getattr(record, "span_id", None)
        if isinstance(trace_id, str) and len(trace_id) == 32:
            payload["trace_id"] = trace_id
        if isinstance(span_id, str) and len(span_id) == 16:
            payload["span_id"] = span_id

        command_kind = getattr(record, "command_kind", None)
        if isinstance(command_kind, str) and command_kind in _SAFE_COMMAND_KINDS:
            payload["command_kind"] = command_kind

        outcome = getattr(record, "outcome", None)
        if isinstance(outcome, str) and outcome in _SAFE_OUTCOMES:
            payload["outcome"] = outcome

        generation = getattr(record, "route_generation", None)
        if type(generation) is int and generation > 0:
            payload["route_generation"] = generation

        retry_after = getattr(record, "retry_after_seconds", None)
        if type(retry_after) is int and retry_after > 0:
            payload["retry_after_seconds"] = retry_after

        exception_type = getattr(record, "exception_type", None)
        if isinstance(exception_type, str) and exception_type.isidentifier() and len(exception_type) <= 80:
            payload["exception_type"] = exception_type

        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_structured_logging(
    *,
    logger_name: str = "tgmate.gateway",
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure one idempotent handler for the Gateway logger namespace."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not any(getattr(handler, "_tgmate_safe_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.addFilter(TraceContextFilter())
        handler.setFormatter(SafeJsonFormatter())
        handler._tgmate_safe_json = True
        logger.addHandler(handler)
    return logger
