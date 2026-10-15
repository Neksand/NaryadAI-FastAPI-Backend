"""Redact authentication material before Uvicorn/application log formatting."""

import logging
import re
from typing import Any

_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|access_token|refresh_token)=)[^&\s\"'<>]+")
_BEARER_SECRET = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_auth(value: str) -> str:
    return _BEARER_SECRET.sub(r"\1[REDACTED]", _QUERY_SECRET.sub(r"\1[REDACTED]", value))


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_auth(value)
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


class AuthRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        # Preserve Uvicorn AccessFormatter's five-element positional tuple.
        record.args = _redact_value(record.args)
        if record.exc_info:
            record.exc_text = redact_auth(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact_auth(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_auth(record.stack_info)
        return True


def install_auth_redaction() -> None:
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "ai_providers"):
        logger = logging.getLogger(name)
        targets = [logger, *logger.handlers]
        for target in targets:
            if not any(isinstance(item, AuthRedactionFilter) for item in target.filters):
                target.addFilter(AuthRedactionFilter())
