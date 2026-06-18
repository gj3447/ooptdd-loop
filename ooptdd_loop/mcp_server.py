"""MCP server — expose the ooptdd-loop tool registry to agents natively.

Thin wrapper over :mod:`ooptdd_loop.tools`. The ``mcp`` SDK is an OPTIONAL
dependency (extra ``mcp``); the registry itself is pure Python and works without
it, so nothing here is on the offline path. ``logserver_*`` tools bridge to the
upstream oo-mcp log server through ``OO_MCP_URL``.

    pip install ooptdd-loop[mcp]
    python -m ooptdd_loop.mcp_server          # stdio MCP server
    # register with Claude/Codex like any stdio MCP server
"""
from __future__ import annotations

import sys

from .tools import TOOLS


def build_server():
    """Build a FastMCP server registering every tool. Raises ImportError without the SDK."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("ooptdd-loop")
    for t in TOOLS:
        server.add_tool(t.fn, name=t.name, description=t.description)
    return server


def main(argv=None) -> int:
    try:
        server = build_server()
    except ImportError:
        print("the MCP server needs the SDK: pip install ooptdd-loop[mcp]", file=sys.stderr)
        return 1
    server.run()  # stdio transport
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
