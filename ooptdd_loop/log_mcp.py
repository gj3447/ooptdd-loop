# KG: OOPTDD_methodology_v1
"""MCP client for the workspace log server.

The log server already exposes OpenObserve through an MCP endpoint (the same
endpoint used by the ``oo`` CLI). OOPTDD keeps credentials out of-process and
calls that MCP server as an upstream evidence source.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit


# ZeroTier route (10.147.17.x) — the Tailscale route (localhost) is offline on
# this box, so the ZT address is the reachable path to the same openobserve-logs
# MCP server (port 55014, same endpoint logs-host uses). Override via OO_MCP_URL.
DEFAULT_OO_MCP_URL = "http://localhost:55014/mcp"
PROTOCOL_VERSION = "2025-03-26"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class LogMcpError(RuntimeError):
    """Raised when the upstream log MCP server rejects or cannot serve a call."""


def resolve_mcp_url(url: str | None = None) -> str:
    """Return the log MCP endpoint. Secrets stay in the upstream server."""
    return url or os.environ.get("OO_MCP_URL") or DEFAULT_OO_MCP_URL


def display_mcp_url(url: str | None = None) -> str:
    """URL for diagnostics, with any inline password redacted."""
    raw = resolve_mcp_url(url)
    parts = urlsplit(raw)
    if not parts.password:
        return raw
    user = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:***@{host}{port}" if user else f"***@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _post(
    body: dict,
    *,
    url: str,
    session_id: str | None = None,
    timeout: float = 40.0,
    opener=None,
) -> tuple[str | None, str]:
    headers = dict(HEADERS)
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    with open_fn(req, timeout=timeout) as response:
        return response.headers.get("mcp-session-id"), response.read().decode()


def _mcp_payload(raw: str) -> dict:
    text = raw.strip()
    if not text:
        return {}
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    return json.loads(text)


def _initialize(*, url: str, timeout: float, opener=None) -> str | None:
    sid, _ = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ooptdd-loop", "version": "0.1.0"},
            },
        },
        url=url,
        timeout=timeout,
        opener=opener,
    )
    _post(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        url=url,
        session_id=sid,
        timeout=timeout,
        opener=opener,
    )
    return sid


def call_log_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    url: str | None = None,
    timeout: float = 40.0,
    opener=None,
) -> Any:
    """Call a tool on the upstream log MCP server and parse JSON text content."""
    endpoint = resolve_mcp_url(url)
    sid = _initialize(url=endpoint, timeout=timeout, opener=opener)
    _, raw = _post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        url=endpoint,
        session_id=sid,
        timeout=timeout,
        opener=opener,
    )
    payload = _mcp_payload(raw)
    if "error" in payload:
        raise LogMcpError(str(payload["error"]))
    try:
        content = payload["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LogMcpError(f"malformed MCP tool response: {payload!r}") from exc
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def list_log_tools(
    *,
    url: str | None = None,
    timeout: float = 40.0,
    opener=None,
) -> list[dict]:
    endpoint = resolve_mcp_url(url)
    sid = _initialize(url=endpoint, timeout=timeout, opener=opener)
    _, raw = _post(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        url=endpoint,
        session_id=sid,
        timeout=timeout,
        opener=opener,
    )
    payload = _mcp_payload(raw)
    if "error" in payload:
        raise LogMcpError(str(payload["error"]))
    return payload.get("result", {}).get("tools", [])


def safe_call_log_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    url: str | None = None,
    timeout: float = 40.0,
) -> dict:
    """Agent-facing call: never throws, returns a reachability verdict."""
    try:
        return {
            "reachable": True,
            "mcp_url": display_mcp_url(url),
            "tool": name,
            "result": call_log_tool(name, arguments, url=url, timeout=timeout),
        }
    except Exception as exc:
        return {
            "reachable": False,
            "mcp_url": display_mcp_url(url),
            "tool": name,
            "error": str(exc),
        }


def safe_list_log_tools(*, url: str | None = None, timeout: float = 40.0) -> dict:
    try:
        return {
            "reachable": True,
            "mcp_url": display_mcp_url(url),
            "tools": list_log_tools(url=url, timeout=timeout),
        }
    except Exception as exc:
        return {"reachable": False, "mcp_url": display_mcp_url(url), "error": str(exc)}


def logserver_health(stale_minutes: float = 15.0, *, timeout: float = 40.0) -> dict:
    return safe_call_log_tool(
        "ingest_health",
        {"stale_minutes": float(stale_minutes)},
        timeout=timeout,
    )


def logserver_trace(cycle_id: str, minutes_back: float = 60.0, *, timeout: float = 40.0) -> dict:
    return safe_call_log_tool(
        "trace_cycle",
        {"cycle_id": cycle_id, "minutes_back": float(minutes_back)},
        timeout=timeout,
    )


def logserver_query(sql: str, minutes_back: float = 60.0, size: int = 100, *,
                    timeout: float = 40.0) -> dict:
    return safe_call_log_tool(
        "query_logs",
        {"sql": sql, "minutes_back": float(minutes_back), "size": int(size)},
        timeout=timeout,
    )


def logserver_errors(minutes_back: float = 60.0, *, stream: str | None = None,
                     timeout: float = 40.0) -> dict:
    args: dict[str, Any] = {"minutes_back": float(minutes_back)}
    if stream:
        args["stream"] = stream
    return safe_call_log_tool("recent_errors", args, timeout=timeout)
