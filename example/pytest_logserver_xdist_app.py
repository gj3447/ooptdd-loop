"""Pytest/xdist pilot target for a real OpenObserve-backed OOPTDD run."""
from __future__ import annotations

import json
import os

from ooptdd.backends import get_backend


def _backend():
    options = json.loads(os.environ.get("OOPTDD_BACKEND_OPTIONS", "{}"))
    return get_backend(os.environ["OOPTDD_BACKEND"], **options)


def _event(cid: str, event: str) -> dict:
    return {
        "cid": cid,
        "correlation_id": cid,
        "cycle_id": cid,
        "service": "ooptdd-pytest-xdist-pilot",
        "component": "pytest_xdist_real_backend",
        "event": event,
        "operation": event,
        "trace_parent": os.environ.get("TRACEPARENT", ""),
        "pytest_worker": os.environ.get("PYTEST_XDIST_WORKER", "controller"),
    }


def _ship(event: str) -> None:
    cid = os.environ["OOPTDD_CID"]
    _backend().ship([_event(cid, event)])


def emit_worker_one() -> None:
    _ship("pytest_xdist_worker_one_arrived")


def emit_worker_two() -> None:
    _ship("pytest_xdist_worker_two_arrived")
