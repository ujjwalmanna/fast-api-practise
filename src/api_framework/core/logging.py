"""Structured logging with request correlation.

Two ideas make logs useful in production, and both are implemented here:

1. **Correlation.** Every log line emitted while handling a request carries the
   same `request_id`. Without it, concurrent requests interleave and a
   traceback cannot be tied back to the call that caused it. The id lives in a
   `ContextVar`, so it propagates through `await` boundaries without being
   threaded through every function signature.

2. **Machine-readable output.** Deployed environments emit one JSON object per
   line, which log aggregators can index. Locally that is painful to read, so
   `APP_LOG_JSON=false` switches to a compact human format.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

# Populated by CorrelationIdMiddleware; empty outside a request (e.g. at startup).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")

# LogRecord attributes that are always present. Anything else was passed by the
# caller via `extra=` and is promoted to a top-level JSON field.
_STANDARD_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def get_request_id() -> str:
    return request_id_ctx.get()


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if request_id := request_id_ctx.get():
            payload["request_id"] = request_id

        # Promote caller-supplied `extra={...}` fields.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str so UUIDs, datetimes and Decimals never break logging.
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Readable single-line format for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)-34s %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if request_id := request_id_ctx.get():
            base = f"{base}  [rid={request_id[:8]}]"
        return base


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    """Install the root logging configuration. Idempotent.

    Called once from the app factory, before anything else can log.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())

    root = logging.getLogger()
    for existing in root.handlers[:]:  # avoid duplicate lines on reload
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn ships its own handlers; drop them so everything flows through
    # ours and inherits the JSON format and request_id.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # uvicorn.access duplicates our own access log - silence it.
    logging.getLogger("uvicorn.access").disabled = True

    # SQLAlchemy echo is controlled by APP_DB_ECHO, not by the root level.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
