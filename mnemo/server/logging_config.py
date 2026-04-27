"""Structured JSON logging for the Mnemo server.

Stdlib `logging` with a custom formatter that emits one JSON object per record
to the configured stream (stdout in production; Docker/k8s captures it).

Call sites stay as `logger = logging.getLogger(__name__)` and pass structured
fields via `extra={...}`; the formatter merges them into the JSON payload.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
})


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per LogRecord. Extra kwargs are merged at top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Replace root logger handlers with a single JSON-formatted stream handler.

    Idempotent: clears any existing handlers first. Safe to call multiple times.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
