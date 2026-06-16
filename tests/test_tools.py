"""V3 judge — the loop is drivable as introspectable agent tools, offline.

Pre-registered metric (LakatosTree_ooptdd_ontology_20260616 / V3-ai-native-mcp):
  >=6 introspectable tools; the `run` tool's verdict == the library run_loop
  (parity); ontology_lookup returns the EventType — ALL with the MCP SDK absent.
"""
import os

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop import tools
from ooptdd_loop.runner import run_loop
from ooptdd_loop.spec import load_spec

ROOT = os.path.dirname(os.path.dirname(__file__))
MET = os.path.join(ROOT, "example", "requirements.yaml")
ONTO = os.path.join(ROOT, "example", "ontology.yaml")


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def test_at_least_six_introspectable_tools():
    listed = tools.list_tools()
    assert len(listed) >= 6
    names = {t["name"] for t in listed}
    assert {"list_requirements", "run", "verify", "rca", "ontology_lookup",
            "coverage", "drift"} <= names
    for t in listed:                       # every tool self-describes
        assert t["name"] and t["description"] and isinstance(t["parameters"], dict)


def test_run_tool_parity_with_library():
    memory_reset()
    lib = run_loop(load_spec(MET))
    memory_reset()
    via_tool = tools.call("run", spec=MET)
    assert via_tool["complete"] == lib.complete
    assert via_tool["done"] == lib.n_done == 4
    assert via_tool["next_step"] == ""      # complete -> nothing for the agent


def test_list_requirements_tool():
    out = tools.call("list_requirements", spec=MET)
    assert out["spec"] == "requirements"
    assert {r["id"] for r in out["requirements"]} == {"REQ-1", "REQ-2", "REQ-3", "REQ-4"}


def test_ontology_lookup_tool():
    out = tools.call("ontology_lookup", ontology=ONTO, event_type="payment_authorized")
    assert out["found"] is True
    assert out["required"] == ["amount"]
    missing = tools.call("ontology_lookup", ontology=ONTO, event_type="nope")
    assert missing["found"] is False


def test_unknown_tool_raises():
    with pytest.raises(KeyError):
        tools.call("does_not_exist")


def test_mcp_server_module_imports_without_sdk():
    # the wrapper module must import even when the optional `mcp` SDK is absent
    from ooptdd_loop import mcp_server

    assert callable(mcp_server.main)
