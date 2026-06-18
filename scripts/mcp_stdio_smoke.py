#!/usr/bin/env python
"""End-to-end smoke check for the ooptdd-loop stdio MCP server."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
import sys
from typing import Any


def _json_from_tool_result(result: Any) -> dict:
    if not result.content:
        raise AssertionError("MCP tool returned no content")
    text = getattr(result.content[0], "text", None)
    if text is None:
        raise AssertionError(f"MCP tool returned non-text content: {result.content[0]!r}")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise AssertionError(f"MCP tool returned non-object JSON: {payload!r}")
    return payload


async def _roundtrip(args: argparse.Namespace) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.setdefault("FASTMCP_LOG_LEVEL", "ERROR")
    env.setdefault("MCP_LOG_LEVEL", "ERROR")
    params = StdioServerParameters(
        command=args.python,
        args=["-m", args.server_module],
        env=env,
    )
    errlog_cm = nullcontext(sys.stderr) if args.show_server_stderr else open(os.devnull, "w")
    with errlog_cm as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = sorted(t.name for t in listed.tools)
                missing = sorted(set(args.required_tool) - set(tool_names))
                if missing:
                    raise AssertionError(f"missing MCP tools: {missing}")

                rules = _json_from_tool_result(await session.call_tool("methodology_rules", {}))
                validate = _json_from_tool_result(
                    await session.call_tool("validate_spec", {"spec": args.spec})
                )
                harness = _json_from_tool_result(await session.call_tool("harness_profile", {}))
                listed_reqs = _json_from_tool_result(
                    await session.call_tool("list_requirements", {"spec": args.spec})
                )

                calls = {
                    "methodology_rules": rules.get("methodology"),
                    "validate_spec": {
                        "spec": validate.get("spec"),
                        "ok": validate.get("ok"),
                        "requirements": validate.get("requirements"),
                        "contracts": validate.get("contracts"),
                    },
                    "harness_profile": {
                        "family": harness.get("family"),
                        "layers": sorted(harness.get("layers", {}).keys()),
                    },
                    "list_requirements": {
                        "spec": listed_reqs.get("spec"),
                        "requirements": len(listed_reqs.get("requirements", [])),
                        "contracts": len(listed_reqs.get("contracts", [])),
                    },
                }

                if args.run:
                    run_payload = _json_from_tool_result(
                        await session.call_tool("run", {"spec": args.spec, "cid": args.cid})
                    )
                    calls["run"] = {
                        "cid": run_payload.get("cid"),
                        "complete": run_payload.get("complete"),
                        "done": run_payload.get("done"),
                        "total": run_payload.get("total"),
                        "methodology_ok": run_payload.get("methodology_ok"),
                    }
                    if not run_payload.get("complete"):
                        raise AssertionError(f"MCP run did not complete: {calls['run']!r}")

                return {
                    "status": "passed",
                    "server_module": args.server_module,
                    "python": args.python,
                    "spec": args.spec,
                    "tools_count": len(tool_names),
                    "required_tools": args.required_tool,
                    "calls": calls,
                }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--python", default=sys.executable, help="Python executable for the MCP server")
    p.add_argument("--server-module", default="ooptdd_loop.mcp_server")
    p.add_argument("--spec", default="example/requirements.yaml")
    p.add_argument("--cid", default="mcp-stdio-smoke")
    p.add_argument("--run", action="store_true", help="also call the MCP run tool")
    p.add_argument(
        "--show-server-stderr",
        action="store_true",
        help="show MCP server stderr instead of suppressing it",
    )
    p.add_argument(
        "--required-tool",
        action="append",
        default=None,
        help="tool name that must be exposed; repeatable; defaults to every registry tool",
    )
    p.add_argument("--require", action="store_true", help="fail if the MCP SDK is unavailable")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.required_tool is None:
        from ooptdd_loop.tools import list_tools

        args.required_tool = [t["name"] for t in list_tools()]
    try:
        import mcp  # noqa: F401
    except ImportError:
        payload = {
            "status": "skipped",
            "required": args.require,
            "reason": "mcp SDK is not installed",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if args.require else 0

    try:
        payload = asyncio.run(_roundtrip(args))
    except Exception as exc:  # pragma: no cover - exercised by shell smoke failures
        payload = {"status": "failed", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
