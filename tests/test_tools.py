"""V3 judge — the loop is drivable as introspectable agent tools, offline.

Pre-registered metric (LakatosTree_ooptdd_ontology_20260616 / V3-ai-native-mcp):
  >=10 introspectable tools; the `run` tool's verdict == the library run_loop
  (parity); ontology_lookup returns the EventType — ALL with the MCP SDK absent.
"""
import os

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop import log_mcp
from ooptdd_loop import tools
from ooptdd_loop.runner import run_loop
from ooptdd_loop.domain.spec import load_spec

ROOT = os.path.dirname(os.path.dirname(__file__))
MET = os.path.join(ROOT, "example", "requirements.yaml")
OOPTDD = os.path.join(ROOT, "example", "requirements_ooptdd.yaml")
ONTO = os.path.join(ROOT, "example", "ontology.yaml")


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def test_at_least_ten_introspectable_tools():
    listed = tools.list_tools()
    assert len(listed) >= 10
    names = {t["name"] for t in listed}
    assert {"list_requirements", "run", "verify", "rca", "ontology_lookup",
            "coverage", "drift", "validate_spec", "methodology_rules",
            "kg_seed", "harness_profile", "logserver_tools", "logserver_health",
            "logserver_trace", "logserver_query", "logserver_errors",
            "golden_save", "golden_diff"} <= names
    for t in listed:                       # every tool self-describes
        assert t["name"] and t["description"] and isinstance(t["parameters"], dict)


def test_run_tool_parity_with_library():
    memory_reset()
    lib = run_loop(load_spec(MET))
    memory_reset()
    via_tool = tools.call("run", spec=MET)
    assert via_tool["complete"] == lib.complete
    assert via_tool["done"] == lib.n_done == 4
    assert via_tool["methodology_ok"] is True
    assert via_tool["next_step"] == ""      # complete -> nothing for the agent


def test_list_requirements_tool():
    out = tools.call("list_requirements", spec=MET)
    assert out["spec"] == "requirements"
    assert {r["id"] for r in out["requirements"]} == {"REQ-1", "REQ-2", "REQ-3", "REQ-4"}
    assert out["methodology"]["enforce"] is False


def test_validate_spec_tool_for_ooptdd_rules():
    out = tools.call("validate_spec", spec=OOPTDD)
    assert out["ooptdd_enabled"] is True
    assert out["ok"] is True
    assert len(out["checks"]) == 14


def test_methodology_rules_and_kg_seed_tools():
    rules = tools.call("methodology_rules")
    assert rules["methodology"] == "OOPTDD_methodology_v1"
    assert any(r["id"] == "OOPTDD-R09" for r in rules["rules"])

    seed = tools.call("kg_seed")
    assert "MERGE (m:AbstractNode:Methodology" in seed["cypher"]
    assert seed["params"]["method"]["name"] == "OOPTDD_methodology_v1"


def test_harness_profile_tool_maps_three_layers():
    profile = tools.call("harness_profile")
    assert set(profile["layers"]) == {"L_IDE", "L_RT", "L_MC"}
    assert "run" in profile["layers"]["L_RT"]["tools"]
    assert "logserver_trace" in profile["layers"]["L_RT"]["tools"]
    assert "golden_diff" in profile["layers"]["L_RT"]["tools"]
    assert "target.capture.logging" in profile["layers"]["L_IDE"]["surfaces"]
    assert "Longinus ReferenceSite" in profile["layers"]["L_MC"]["surfaces"]
    assert "golden_save" in profile["layers"]["L_MC"]["surfaces"]


def test_logserver_tool_delegates_to_upstream_mcp(monkeypatch):
    def fake_health(stale_minutes):
        return {"reachable": True, "result": {"stale_over_min": stale_minutes}}

    monkeypatch.setattr(log_mcp, "logserver_health", fake_health)
    out = tools.call("logserver_health", stale_minutes=7)
    assert out == {"reachable": True, "result": {"stale_over_min": 7}}


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
