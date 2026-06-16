"""Local structured event capture for in-process OOPTDD runs."""
from __future__ import annotations

import logging
from contextlib import contextmanager

_BASE_RECORD_KEYS = set(
    logging.LogRecord("x", logging.INFO, "x.py", 1, "msg", (), None).__dict__
)
_EXTRA_SKIP = {"message", "asctime"}


class StructuredLogHandler(logging.Handler):
    """Ship structured logging records into an OOPTDD backend."""

    def __init__(self, backend, cid: str, *, service: str | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.cid = cid
        self.service = service
        self.events: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        event = normalize_log_record(record, self.cid, service=self.service)
        if event is None:
            return
        self.events.append(event)
        self.backend.ship([event])


@contextmanager
def capture_logging_to_backend(
    backend,
    cid: str,
    *,
    logger_name: str | None = None,
    level: int | str = logging.INFO,
    service: str | None = None,
):
    """Capture structured Python logging records and ship them to ``backend``."""
    logger = logging.getLogger(logger_name)
    old_level = logger.level
    handler = StructuredLogHandler(backend, cid, service=service)
    handler.setLevel(_levelno(level))
    logger.addHandler(handler)
    if old_level == logging.NOTSET or old_level > handler.level:
        logger.setLevel(handler.level)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def structlog_event_processor(backend, cid: str, *, service: str | None = None):
    """Return a structlog-compatible processor that also ships the event dict.

    It intentionally does not import structlog; callers can insert the returned
    callable into a structlog processor chain when structlog is present.
    """

    def processor(logger, method_name: str, event_dict: dict):
        payload = dict(event_dict)
        _envelope(payload, cid, service=service)
        payload.setdefault("level", method_name)
        if _event_name(payload):
            backend.ship([payload])
        return event_dict

    return processor


def normalize_log_record(
    record: logging.LogRecord,
    cid: str,
    *,
    service: str | None = None,
) -> dict | None:
    if isinstance(record.msg, dict):
        payload = dict(record.msg)
    else:
        payload = _record_extra(record)
        if "event" not in payload and "event_type" not in payload:
            return None
        payload.setdefault("message", record.getMessage())

    _envelope(payload, cid, service=service or record.name)
    payload.setdefault("level", record.levelname.lower())
    payload.setdefault("logger", record.name)
    return payload if _event_name(payload) else None


def _record_extra(record: logging.LogRecord) -> dict:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _BASE_RECORD_KEYS and key not in _EXTRA_SKIP
    }


def _envelope(payload: dict, cid: str, *, service: str | None) -> None:
    payload.setdefault("cid", cid)
    payload.setdefault("correlation_id", cid)
    payload.setdefault("cycle_id", cid)
    if service:
        payload.setdefault("service", service)
    if "event" not in payload and payload.get("event_type"):
        payload["event"] = payload["event_type"]


def _event_name(payload: dict) -> str | None:
    return payload.get("event") or payload.get("event_type")


def _levelno(level: int | str) -> int:
    if isinstance(level, int):
        return level
    return logging._nameToLevel.get(level.upper(), logging.INFO)
