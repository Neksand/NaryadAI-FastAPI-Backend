import io
import logging

from app.logging_utils import AuthRedactionFilter, redact_auth


def test_websocket_token_and_bearer_are_redacted_without_changing_safe_fields():
    text = "WebSocket /api/ws?token=synthetic.secret.signature&limit=5 Bearer synthetic.auth.token"
    redacted = redact_auth(text)
    assert "synthetic" not in redacted
    assert "limit=5" in redacted
    assert redacted.count("[REDACTED]") == 2


def test_uvicorn_access_positional_fields_keep_formatter_compatibility():
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/api/ws?token=synthetic.secret", "1.1", 101),
        None,
    )
    AuthRedactionFilter().filter(record)
    formatted = AccessFormatter("%(request_line)s %(status_code)s", use_colors=False).format(record)
    assert "synthetic.secret" not in formatted
    assert "[REDACTED]" in formatted and "101" in formatted


def test_exception_traceback_redacts_token_query():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(AuthRedactionFilter())
    logger = logging.getLogger("test.naryadai.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    try:
        raise RuntimeError("failed request /api/ws?token=synthetic.secret")
    except RuntimeError:
        logger.exception("Request failed")
    assert "synthetic.secret" not in stream.getvalue()
    assert "RuntimeError" in stream.getvalue()
    logger.handlers = []
