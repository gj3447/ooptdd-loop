import json

import pytest

from ooptdd_loop import log_mcp, oo_rca


class _Response:
    def __init__(self, raw: str, *, session_id: str | None = None):
        self.raw = raw
        self.headers = {}
        if session_id:
            self.headers["mcp-session-id"] = session_id

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self.raw.encode()


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def test_call_log_tool_uses_streamable_http_mcp():
    calls = []

    def opener(req, timeout):
        body = json.loads(req.data.decode())
        calls.append(body)
        if body["method"] == "initialize":
            return _Response(
                _sse({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            )
        if body["method"] == "notifications/initialized":
            return _Response("")
        if body["method"] == "tools/call":
            result = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"hits": [{"event": "payment_authorized"}]}),
                        }
                    ]
                },
            }
            return _Response(_sse(result))
        raise AssertionError(body)

    out = log_mcp.call_log_tool(
        "trace_cycle",
        {"cycle_id": "c1", "minutes_back": 5},
        url="http://example.invalid/mcp",
        opener=opener,
    )

    assert out == {"hits": [{"event": "payment_authorized"}]}
    assert [c["method"] for c in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert calls[-1]["params"]["name"] == "trace_cycle"
    assert calls[-1]["params"]["arguments"]["cycle_id"] == "c1"


def test_list_log_tools_uses_mcp_tools_list():
    def opener(req, timeout):
        body = json.loads(req.data.decode())
        if body["method"] == "initialize":
            return _Response(
                _sse({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            )
        if body["method"] == "notifications/initialized":
            return _Response("")
        if body["method"] == "tools/list":
            return _Response(
                _sse({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "trace_cycle"}]},
                })
            )
        raise AssertionError(body)

    assert log_mcp.list_log_tools(url="http://example.invalid/mcp", opener=opener) == [
        {"name": "trace_cycle"}
    ]


def test_safe_log_call_reports_unreachable(monkeypatch):
    def down(*_args, **_kwargs):
        raise RuntimeError("log mcp down")

    monkeypatch.setattr(log_mcp, "call_log_tool", down)
    out = log_mcp.safe_call_log_tool("trace_cycle", {"cycle_id": "c1"})
    assert out["reachable"] is False
    assert "log mcp down" in out["error"]


def test_rca_prefers_logserver_mcp_trace(monkeypatch):
    def fake_trace(cid, *, minutes_back, timeout):
        assert cid == "c1"
        return {"reachable": True, "result": {"hits": [{"event": "via_mcp"}]}}

    monkeypatch.setattr(oo_rca, "logserver_trace", fake_trace)
    assert oo_rca.oo_trace_summary("c1") == {"hits": [{"event": "via_mcp"}]}


def test_call_log_tool_raises_on_mcp_error():
    def opener(req, timeout):
        body = json.loads(req.data.decode())
        if body["method"] == "initialize":
            return _Response(
                _sse({"jsonrpc": "2.0", "id": 1, "result": {}}),
                session_id="sid-1",
            )
        if body["method"] == "notifications/initialized":
            return _Response("")
        return _Response(_sse({"jsonrpc": "2.0", "id": 2, "error": {"message": "bad"}}))

    with pytest.raises(log_mcp.LogMcpError):
        log_mcp.call_log_tool("trace_cycle", url="http://example.invalid/mcp", opener=opener)
