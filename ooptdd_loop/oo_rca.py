"""AI-friendly root-cause context, log-grounded.

When a requirement is RED, the agent should not guess — it should read what the
store actually saw. This module turns the store's view of a correlation id into a
compact, aggregation-first summary (never a raw dump) the agent can act on.

Two sources, in order:
  1. the ``oo`` CLI (oo-mcp client) — ``oo trace <cid>`` for the full cross-stream
     timeline — when available (the real oo-mcp path).
  2. otherwise the configured backend's own query (works offline with memory).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time


def _have_oo() -> bool:
    return shutil.which("oo") is not None


def oo_trace_summary(cid: str, *, minutes: int = 60, timeout: float = 20.0) -> dict | None:
    """`oo trace <cid> --json` parsed, or None if oo is unavailable/failed."""
    if not _have_oo():
        return None
    try:
        out = subprocess.run(
            ["oo", "trace", cid, str(minutes), "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def _counts(events: list[dict]) -> dict:
    by_event, by_level = {}, {}
    for e in events:
        by_event[e.get("event", "?")] = by_event.get(e.get("event", "?"), 0) + 1
        by_level[e.get("level", "?")] = by_level.get(e.get("level", "?"), 0) + 1
    return {"by_event": by_event, "by_level": by_level, "total": len(events)}


def rca_block(backend, cid: str, *, mode: str, want_events: list[str]) -> str:
    """Aggregation-first RCA text for a failing requirement."""
    lines = [f"RCA for cid={cid} (mode={mode})", f"  expected events: {want_events}"]

    if mode == "openobserve":
        tr = oo_trace_summary(cid)
        if tr is not None:
            hits = tr.get("hits") or tr.get("events") or []
            c = _counts(hits) if isinstance(hits, list) else {}
            lines.append(f"  oo trace_cycle: {c.get('total', '?')} records "
                         f"(by_event={c.get('by_event', {})}, by_level={c.get('by_level', {})})")
            errs = [h for h in hits if h.get("level") in ("ERROR", "CRITICAL")] if isinstance(hits, list) else []
            for e in errs[:3]:
                lines.append(f"  ERROR: {str(e.get('error') or e.get('event'))[:200]}")
            _diagnose(lines, c.get("by_event", {}), want_events)
            return "\n".join(lines)
        lines.append("  (oo CLI unavailable — falling back to backend query)")

    now_us = int(time.time() * 1_000_000)
    res = backend.query(cid, since_us=now_us - backend.default_lookback_s * 1_000_000,
                        until_us=now_us + backend.default_future_buffer_s * 1_000_000)
    if not res.reachable:
        lines.append("  store UNREACHABLE -> verdict inconclusive (?): infra problem, "
                     "not the code. Check the store/credentials, do NOT 'fix' the code.")
        return "\n".join(lines)
    c = _counts(res.events)
    lines.append(f"  store saw {c['total']} records (by_event={c['by_event']})")
    _diagnose(lines, c["by_event"], want_events)
    return "\n".join(lines)


def _diagnose(lines: list[str], by_event: dict, want_events: list[str]):
    missing = [e for e in want_events if by_event.get(e, 0) == 0]
    if missing:
        lines.append(f"  -> MISSING entirely: {missing}. Either the code path never ran, "
                     "or the events are not emitted under this cid. Check propagation of the "
                     "correlation id and that the emitting symbol is actually reached.")
    else:
        lines.append("  -> all expected events appear at least once; the failure is a COUNT "
                     "mismatch (wrong cardinality) or partial loss. Compare expected op/count.")
