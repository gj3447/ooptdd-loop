import os

from ooptdd_loop import cli
from ooptdd_loop.cli import main

ROOT = os.path.dirname(os.path.dirname(__file__))
OOPTDD = os.path.join(ROOT, "example", "requirements_ooptdd.yaml")


def test_cli_tools_lists_harness_registry(capsys):
    assert main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "validate_spec" in out
    assert "harness_profile" in out
    assert "golden_diff" in out


def test_cli_validate_spec_for_ooptdd(capsys):
    assert main(["validate-spec", OOPTDD]) == 0
    out = capsys.readouterr().out
    assert "shop-ooptdd-demo: PASS" in out


def test_cli_harness_profile(capsys):
    assert main(["harness-profile"]) == 0
    out = capsys.readouterr().out
    assert "L_IDE" in out and "L_RT" in out and "L_MC" in out


def test_cli_mcp_check(capsys):
    assert main(["mcp", "--check"]) == 0
    out = capsys.readouterr().out
    assert "ooptdd-loop-mcp" in out
    assert "validate_spec" in out
    assert "logserver_upstream_mcp" in out
    assert "logserver_trace" in out
    assert "golden_diff" in out


def test_cli_logserver_health_uses_tool_surface(monkeypatch, capsys):
    def fake_health(stale_minutes):
        return {"reachable": True, "result": {"stale_over_min": stale_minutes}}

    monkeypatch.setattr(cli, "t_logserver_health", fake_health)
    assert main(["logserver-health", "--stale-minutes", "3"]) == 0
    out = capsys.readouterr().out
    assert '"stale_over_min": 3.0' in out
