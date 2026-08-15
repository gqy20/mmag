"""Safe structured logging and request-scoped correlation context."""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

PKG_NAME = "mmag"
_DEFAULT_LOG_DIR = "logs"
_DEFAULT_RETAIN_DAYS = 30
_CONTEXT_FIELDS = (
    "trace_id",
    "workflow_id",
    "task_id",
    "run_id",
    "parent_run_id",
    "thread_id",
    "checkpoint_id",
    "span_id",
    "parent_span_id",
    "conversation_id",
    "actor_id",
    "scope_id",
    "agent_ref",
    "skill_ref",
    "capability",
    "capability_call_id",
    "approval_id",
    "artifact_id",
    "execution_key",
    "policy_ref",
    "delivery_id",
)
_EVENT_FIELDS = (
    "schema_version",
    "event_id",
    "event",
    "status",
    "duration_ms",
    "error_code",
    "attempt",
    "input_sha256",
    "output_size",
)
_SECRET_KEY = re.compile(
    r"(?i)(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret|signature)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|((?:api[-_]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+"
)
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*(#[^\s]*)?")
_initialized = False


def _utc_time(seconds: float | None = None) -> time.struct_time:
    return time.gmtime(seconds)


class LogContext:
    """Immutable, nestable logging fields propagated through async ContextVars."""

    def __init__(self) -> None:
        self._values: ContextVar[Mapping[str, str] | None] = ContextVar(
            "mmag_log_context", default=None
        )

    @contextmanager
    def bind(self, **fields: Any) -> Iterator[None]:
        values = {
            **(self._values.get() or {}),
            **{
                key: str(value)
                for key, value in fields.items()
                if value not in (None, "")
            },
        }
        token = self._values.set(values)
        try:
            yield
        finally:
            self._values.reset(token)

    def snapshot(self) -> dict[str, str]:
        return dict(self._values.get() or {})

    def get(self, name: str, default: str = "") -> str:
        return (self._values.get() or {}).get(name, default)

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex[:16]


log_context = LogContext()


def get_logger(name: str) -> logging.Logger:
    if name == PKG_NAME or name.startswith(f"{PKG_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{PKG_NAME}.{name}")


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    status: str = "",
    **fields: Any,
) -> None:
    """Emit one machine-queryable event without putting business content in the message."""
    reserved = set((*_CONTEXT_FIELDS, *_EVENT_FIELDS))
    extra = {
        "schema_version": "1.0",
        "event_id": uuid.uuid4().hex,
        "event": event,
        "status": status,
        **{key: value for key, value in fields.items() if key in reserved},
        "details": {key: value for key, value in fields.items() if key not in reserved},
    }
    logger.log(level, event, extra=extra)


def safe_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    import hashlib

    return hashlib.sha256(encoded).hexdigest()[:16]


def _redact(value: Any, *, key: str = "") -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = _SECRET_VALUE.sub(lambda match: f"{match.group(1) or match.group(2)}[REDACTED]", value)
    return _URL_QUERY.sub(r"\1?[REDACTED]\2", redacted)


class ContextFilter(logging.Filter):
    """Attach correlation fields and redact every record before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = log_context.snapshot()
        for name in _CONTEXT_FIELDS:
            current = getattr(record, name, "")
            setattr(record, name, _redact(current or context.get(name, ""), key=name))
        for name in _EVENT_FIELDS:
            setattr(record, name, _redact(getattr(record, name, ""), key=name))
        record.details = _redact(getattr(record, "details", {}))
        record.msg = _redact(record.msg)
        record.args = _redact(record.args)
        if record.exc_info and record.exc_info[0] is not None:
            record.error_type = record.exc_info[0].__name__
        else:
            record.error_type = str(getattr(record, "error_type", ""))
        return True


class SafeTextFormatter(logging.Formatter):
    converter = staticmethod(_utc_time)

    def formatException(self, exc_info) -> str:  # noqa: N802 - logging API
        frames = traceback.extract_tb(exc_info[2])
        locations = " <- ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames[-8:]
        )
        error_type = exc_info[0].__name__ if exc_info[0] is not None else "Exception"
        return f"{error_type} at {locations}" if locations else error_type


class JSONFormatter(logging.Formatter):
    converter = staticmethod(_utc_time)

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", "") or "log.message",
            "message": record.getMessage(),
        }
        for name in (*_CONTEXT_FIELDS, *_EVENT_FIELDS, "error_type"):
            value = getattr(record, name, "")
            if value not in (None, ""):
                payload[name] = value
        details = getattr(record, "details", {})
        if details:
            payload["details"] = details
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def init_logging(
    level: str = "INFO",
    log_dir: str | None = _DEFAULT_LOG_DIR,
    retain_days: int = _DEFAULT_RETAIN_DAYS,
    *,
    log_format: str = "text",
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Initialize stdout and optional rotating files exactly once."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root_logger = logging.getLogger(PKG_NAME)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    _close_handlers(root_logger)
    formatter: logging.Formatter = (
        JSONFormatter()
        if log_format.lower() == "json"
        else SafeTextFormatter(
            "%(asctime)sZ [%(levelname)s] %(name)s event=%(event)s "
            "trace=%(trace_id)s run=%(run_id)s agent=%(agent_ref)s %(message)s details=%(details)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    context_filter = ContextFilter()
    console = logging.StreamHandler(sys.stdout)
    _configure_handler(console, formatter, context_filter)
    root_logger.addHandler(console)

    effective_dir = log_dir.strip() if isinstance(log_dir, str) else ""
    if effective_dir:
        path = _session_log_path(effective_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        _configure_handler(file_handler, JSONFormatter(), context_filter)
        root_logger.addHandler(file_handler)
        _cleanup_old_logs(effective_dir, retain_days)
    root_logger.propagate = False
    log_event(
        get_logger(__name__),
        "logging.started",
        status="ready",
        log_format=log_format,
        file_output=bool(effective_dir),
        retention_days=retain_days,
    )


def shutdown_logging() -> None:
    global _initialized
    _close_handlers(logging.getLogger(PKG_NAME))
    _initialized = False


def _configure_handler(
    handler: logging.Handler,
    formatter: logging.Formatter,
    context_filter: logging.Filter,
) -> None:
    handler.setLevel(logging.DEBUG)
    handler.addFilter(context_filter)
    handler.setFormatter(formatter)


def _close_handlers(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def _session_log_path(log_dir: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(log_dir) / f"{PKG_NAME}-{timestamp}-{os.getpid()}.log"


def _cleanup_old_logs(log_dir: str, retain_days: int) -> None:
    if retain_days <= 0:
        return
    cutoff = datetime.now(UTC) - timedelta(days=retain_days)
    for filename in glob.glob(str(Path(log_dir) / f"{PKG_NAME}-*.log*")):
        path = Path(filename)
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if modified < cutoff:
                path.unlink()
        except OSError:
            continue
