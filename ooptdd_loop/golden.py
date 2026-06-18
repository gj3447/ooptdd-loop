"""Golden trace baselines for OOPTDD runs."""
from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

from ooptdd.backends import get_backend

from .runner import _new_cid, evaluate_requirements, run_loop
from .domain.spec import Spec

SCHEMA_VERSION = "ooptdd.golden.v1"
VOLATILE_EVENT_KEYS = {
    "_timestamp",
    "cid",
    "correlation_id",
    "cycle_id",
    "trace_id",
    "span_id",
    "parent_span_id",
}
IDENTITY_KEYS = ("event", "service", "operation")


def save_golden(
    spec: Spec,
    *,
    out: str,
    cid: str | None = None,
    run: bool = False,
    allow_incomplete: bool = False,
) -> dict:
    """Save a normalized event baseline and requirement verdict."""
    snapshot = capture_snapshot(spec, cid=cid, run=run)
    if not snapshot["complete"] and not allow_incomplete:
        raise ValueError("cannot save golden baseline from incomplete run; pass allow_incomplete=True")
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {**snapshot, "path": str(path)}


def diff_golden(
    spec: Spec,
    *,
    baseline: str,
    cid: str | None = None,
    run: bool = False,
) -> dict:
    """Compare a current run against a saved golden baseline."""
    baseline_payload = load_golden(baseline)
    current = capture_snapshot(spec, cid=cid, run=run)
    changes = _changes(baseline_payload, current)
    status = _status(current, changes)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "passed": status == "PASSED",
        "baseline": baseline,
        "baseline_cid": baseline_payload.get("cid"),
        "cid": current["cid"],
        "spec": current["spec"],
        "complete": current["complete"],
        "changes": changes,
        "baseline_summary": _summary(baseline_payload),
        "current_summary": _summary(current),
    }


def load_golden(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported golden schema {payload.get('schema_version')!r}")
    return payload


def capture_snapshot(spec: Spec, *, cid: str | None = None, run: bool = False) -> dict:
    cid = cid or _new_cid("golden")
    if run:
        result = run_loop(spec, cid=cid)
    else:
        result = evaluate_requirements(spec, cid=cid)
    events = _query_events(spec, cid)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now_iso(),
        "spec": spec.name,
        "cid": cid,
        "backend": spec.target.backend,
        "complete": result.complete,
        "done": result.n_done,
        "total": len(result.results),
        "requirements": _requirements(result),
        "event_identities": [_event_identity(event) for event in events],
        "events": [_normalize_event(event) for event in events],
    }


def default_golden_path(spec: Spec) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in spec.name)
    return os.path.join(".ooptdd", "golden", f"{safe_name}.json")


def _query_events(spec: Spec, cid: str) -> list[dict]:
    backend = get_backend(spec.target.backend, **spec.target.backend_options)
    now_us = int(time.time() * 1_000_000)
    result = backend.query(
        cid,
        since_us=now_us - backend.default_lookback_s * 1_000_000,
        until_us=now_us + backend.default_future_buffer_s * 1_000_000,
    )
    if not result.reachable:
        return []
    return result.events


def _requirements(result) -> list[dict]:
    return [
        {
            "id": r.id,
            "gate_ok": r.gate_ok,
            "reachable": r.reachable,
            "bound": r.bound,
            "done": r.done,
            "checks": _stable(r.checks),
            "binding": _stable(dataclasses.asdict(r.binding)) if r.binding else None,
        }
        for r in result.results
    ]


def _normalize_event(event: dict) -> dict:
    attrs = {
        key: _stable(value)
        for key, value in sorted(event.items())
        if key not in VOLATILE_EVENT_KEYS and key not in IDENTITY_KEYS
    }
    normalized = {key: event.get(key) for key in IDENTITY_KEYS if event.get(key) is not None}
    normalized["attrs"] = attrs
    return normalized


def _event_identity(event: dict) -> dict:
    return {key: event.get(key) for key in IDENTITY_KEYS if event.get(key) is not None}


def _stable(value: Any):
    if isinstance(value, dict):
        return {
            str(key): _stable(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
            if key not in VOLATILE_EVENT_KEYS
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, tuple):
        return [_stable(item) for item in value]
    return value


def _changes(baseline: dict, current: dict) -> list[dict]:
    changes: list[dict] = []
    if _requirement_verdicts(baseline) != _requirement_verdicts(current):
        changes.append(
            {
                "kind": "requirement_verdict",
                "baseline": _requirement_verdicts(baseline),
                "current": _requirement_verdicts(current),
            }
        )
    if baseline.get("event_identities") != current.get("event_identities"):
        changes.append(
            {
                "kind": "event_identity_sequence",
                "baseline": baseline.get("event_identities", []),
                "current": current.get("event_identities", []),
            }
        )
    elif baseline.get("events") != current.get("events"):
        changes.append(
            {
                "kind": "event_payload",
                "baseline": baseline.get("events", []),
                "current": current.get("events", []),
            }
        )
    return changes


def _status(current: dict, changes: list[dict]) -> str:
    if not current.get("complete") or any(c["kind"] == "requirement_verdict" for c in changes):
        return "REGRESSION"
    if any(c["kind"] == "event_identity_sequence" for c in changes):
        return "TOOLS_CHANGED"
    if any(c["kind"] == "event_payload" for c in changes):
        return "OUTPUT_CHANGED"
    return "PASSED"


def _requirement_verdicts(snapshot: dict) -> list[dict]:
    return [
        {
            "id": req["id"],
            "gate_ok": req["gate_ok"],
            "bound": req["bound"],
            "done": req["done"],
        }
        for req in snapshot.get("requirements", [])
    ]


def _summary(snapshot: dict) -> dict:
    return {
        "spec": snapshot.get("spec"),
        "cid": snapshot.get("cid"),
        "complete": snapshot.get("complete"),
        "done": snapshot.get("done"),
        "total": snapshot.get("total"),
        "events": len(snapshot.get("events", [])),
    }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
