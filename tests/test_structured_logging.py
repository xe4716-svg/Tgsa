import json
import logging
from io import StringIO

from opentelemetry.sdk.trace import TracerProvider

from app.structured_logging import configure_structured_logging


def test_json_logs_include_trace_context_and_only_allowlisted_metadata():
    output=StringIO()
    logger=configure_structured_logging(logger_name="tgmate.gateway.test.safe",stream=output)
    provider=TracerProvider()
    try:
        with provider.get_tracer("structured-logging-test").start_as_current_span("gateway.command.execute") as span:
            logger.info(
                "PRIVATE MESSAGE BODY MUST NOT BE SERIALIZED",
                extra={
                    "event_name":"gateway.command.completed",
                    "command_kind":"preflight_send_text",
                    "outcome":"SUCCEEDED",
                    "route_generation":9,
                    "account_id":"private-account-id",
                    "tenant_id":"private-tenant-id",
                    "operation_key":"private-operation-key",
                    "payload":"private-message-body",
                    "session_string":"private-session",
                },
            )
            expected_trace_id=f"{span.get_span_context().trace_id:032x}"
            expected_span_id=f"{span.get_span_context().span_id:016x}"
        record=json.loads(output.getvalue())
        assert record["event"]=="gateway.command.completed"
        assert record["trace_id"]==expected_trace_id
        assert record["span_id"]==expected_span_id
        assert record["command_kind"]=="preflight_send_text"
        assert record["outcome"]=="SUCCEEDED"
        assert record["route_generation"]==9
        rendered=output.getvalue()
        for secret in (
            "PRIVATE MESSAGE BODY",
            "private-account-id",
            "private-tenant-id",
            "private-operation-key",
            "private-message-body",
            "private-session",
        ):
            assert secret not in rendered
    finally:
        provider.shutdown()


def test_formatter_drops_unknown_event_names_and_unbounded_extra_fields():
    output=StringIO()
    logger=configure_structured_logging(logger_name="tgmate.gateway.test.unknown",stream=output)
    logger.info(
        "untrusted text should not be emitted",
        extra={
            "event_name":"arbitrary.user.controlled.event",
            "outcome":"SOME_UNKNOWN_STATUS",
            "destination_peer_id":123456,
            "request_url":"/accounts/private-id",
        },
    )
    record=json.loads(output.getvalue())
    assert record["event"]=="application.log"
    assert "outcome" not in record
    assert "destination_peer_id" not in record
    assert "request_url" not in record
    assert "untrusted text" not in output.getvalue()


def test_configure_structured_logging_is_idempotent():
    logger=configure_structured_logging(logger_name="tgmate.gateway.test.idempotent",stream=StringIO())
    first=len(logger.handlers)
    configure_structured_logging(logger_name="tgmate.gateway.test.idempotent",stream=StringIO())
    assert len(logger.handlers)==first==1
    assert getattr(logger.handlers[0],"_tgmate_safe_json",False)
