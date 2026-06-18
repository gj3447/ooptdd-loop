import asyncio
import json
import sys

import pytest


pytest.importorskip("mcp")


async def _roundtrip():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ooptdd_loop.mcp_server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            result = await session.call_tool("methodology_rules", {})
            return names, json.loads(result.content[0].text)


def test_stdio_mcp_exposes_ooptdd_and_logserver_tools():
    names, rules = asyncio.run(_roundtrip())
    assert "run" in names
    assert "logserver_health" in names
    assert "logserver_trace" in names
    assert rules["methodology"] == "OOPTDD_methodology_v1"
