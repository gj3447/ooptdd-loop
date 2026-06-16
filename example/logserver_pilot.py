"""OOPTDD pilot target for the upstream log-server MCP bridge.

The target proves a real integration path:
  OOPTDD target -> upstream oo-mcp logserver -> OOPTDD backend.ship -> store readback.
"""
from __future__ import annotations

import os

from ooptdd_loop.log_mcp import logserver_health


def _fake_health() -> dict:
    return {
        "reachable": True,
        "mcp_url": "fake://logserver",
        "result": {
            "stale_over_min": 15.0,
            "stale_streams": [],
            "streams": {"tests": {"lag_min": 0.0}},
        },
    }


def _event(cid: str, event: str, **attrs) -> dict:
    return {
        "cid": cid,
        "correlation_id": cid,
        "cycle_id": cid,
        "service": "ooptdd-loop",
        "component": "logserver_mcp_bridge",
        "event": event,
        **attrs,
    }


def check_logserver_health(backend, cid: str) -> dict:
    """Query upstream oo-mcp health and emit the OOPTDD proof event."""
    if os.getenv("OOPTDD_PILOT_FAKE_HEALTH"):
        health = _fake_health()
    else:
        health = logserver_health(stale_minutes=float(os.getenv("OOPTDD_STALE_MINUTES", "15")))

    result = health.get("result") if isinstance(health, dict) else None
    streams = result.get("streams", {}) if isinstance(result, dict) else {}
    stale_streams = result.get("stale_streams", []) if isinstance(result, dict) else []
    reachable = bool(health.get("reachable")) if isinstance(health, dict) else False

    backend.ship([
        _event(
            cid,
            "logserver_health_checked",
            reachable=reachable,
            stream_count=len(streams),
            stale_count=len(stale_streams),
            mcp_url=health.get("mcp_url", "") if isinstance(health, dict) else "",
        )
    ])
    return {"reachable": reachable, "stream_count": len(streams), "stale_count": len(stale_streams)}
