import json

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.cli import main
from ooptdd_loop.golden import diff_golden, save_golden
from ooptdd_loop.domain.spec import load_spec
from ooptdd_loop.tools import call


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _write_app(tmp_path, name: str, body: str) -> str:
    path = tmp_path / f"{name}.py"
    path.write_text(
        f"""
def ev(cid, event, service="checkout", operation="run", **attrs):
    return {{
        "cid": cid,
        "correlation_id": cid,
        "cycle_id": cid,
        "event": event,
        "service": service,
        "operation": operation,
        **attrs,
    }}


def run_pipeline(backend, cid):
{body}
""",
        encoding="utf-8",
    )
    return name


def _write_spec(tmp_path, module: str):
    spec = tmp_path / f"{module}.yaml"
    spec.write_text(
        f"""
name: golden-demo
target:
  mode: in_process
  callable: {module}:run_pipeline
  backend: memory
  root: {tmp_path}
requirements:
  - id: REQ-PAY
    description: payment is authorized
    gate:
      - {{event: payment_authorized, op: "==", count: 1}}
    longinus:
      kg_anchor: ref_site:golden:payment
      source: {module}.py
      symbol: run_pipeline
      must_emit: payment_authorized
""",
        encoding="utf-8",
    )
    return spec


def _scenario(tmp_path, name: str, lines: list[str]):
    body = "\n".join(f"    {line}" for line in lines)
    module = _write_app(tmp_path, name, body)
    return _write_spec(tmp_path, module)


def test_golden_save_and_diff_pass_for_same_trace(tmp_path):
    spec = _scenario(
        tmp_path,
        "golden_same",
        [
            'backend.ship([ev(cid, "order_received", amount=42)])',
            'backend.ship([ev(cid, "payment_authorized", amount=42)])',
        ],
    )
    baseline = tmp_path / "golden.json"

    saved = save_golden(load_spec(str(spec)), out=str(baseline), cid="golden-base", run=True)
    diff = diff_golden(load_spec(str(spec)), baseline=str(baseline), cid="golden-next", run=True)

    assert saved["complete"] is True
    assert saved["events"][1]["event"] == "payment_authorized"
    assert diff["status"] == "PASSED"
    assert diff["passed"] is True
    assert diff["changes"] == []


def test_golden_diff_classifies_tool_sequence_change(tmp_path):
    base = _scenario(
        tmp_path,
        "golden_base_tools",
        [
            'backend.ship([ev(cid, "order_received", amount=42)])',
            'backend.ship([ev(cid, "payment_authorized", amount=42)])',
        ],
    )
    changed = _scenario(
        tmp_path,
        "golden_changed_tools",
        [
            'backend.ship([ev(cid, "order_received", amount=42)])',
            'backend.ship([ev(cid, "fraud_checked", amount=42)])',
            'backend.ship([ev(cid, "payment_authorized", amount=42)])',
        ],
    )
    baseline = tmp_path / "golden.json"
    save_golden(load_spec(str(base)), out=str(baseline), cid="golden-tool-base", run=True)

    diff = diff_golden(load_spec(str(changed)), baseline=str(baseline),
                       cid="golden-tool-next", run=True)

    assert diff["status"] == "TOOLS_CHANGED"
    assert any(c["kind"] == "event_identity_sequence" for c in diff["changes"])


def test_golden_diff_classifies_output_change(tmp_path):
    base = _scenario(
        tmp_path,
        "golden_base_output",
        ['backend.ship([ev(cid, "payment_authorized", amount=42)])'],
    )
    changed = _scenario(
        tmp_path,
        "golden_changed_output",
        ['backend.ship([ev(cid, "payment_authorized", amount=99)])'],
    )
    baseline = tmp_path / "golden.json"
    save_golden(load_spec(str(base)), out=str(baseline), cid="golden-output-base", run=True)

    diff = diff_golden(load_spec(str(changed)), baseline=str(baseline),
                       cid="golden-output-next", run=True)

    assert diff["status"] == "OUTPUT_CHANGED"
    assert any(c["kind"] == "event_payload" for c in diff["changes"])


def test_golden_diff_classifies_regression_when_requirement_is_red(tmp_path):
    base = _scenario(
        tmp_path,
        "golden_base_regression",
        ['backend.ship([ev(cid, "payment_authorized", amount=42)])'],
    )
    broken = _scenario(
        tmp_path,
        "golden_broken_regression",
        ['backend.ship([ev(cid, "order_received", amount=42)])'],
    )
    baseline = tmp_path / "golden.json"
    save_golden(load_spec(str(base)), out=str(baseline), cid="golden-reg-base", run=True)

    diff = diff_golden(load_spec(str(broken)), baseline=str(baseline),
                       cid="golden-reg-next", run=True)

    assert diff["status"] == "REGRESSION"
    assert diff["passed"] is False
    assert any(c["kind"] == "requirement_verdict" for c in diff["changes"])


def test_golden_cli_and_tool_surface(tmp_path, capsys):
    spec = _scenario(
        tmp_path,
        "golden_cli",
        ['backend.ship([ev(cid, "payment_authorized", amount=42)])'],
    )
    baseline = tmp_path / "golden.json"

    assert main(["golden", "save", str(spec), "--out", str(baseline), "--cid", "golden-cli-base",
                 "--run"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["path"] == str(baseline)

    assert main(["golden", "diff", str(spec), str(baseline), "--cid", "golden-cli-next",
                 "--run"]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["status"] == "PASSED"

    tool_diff = call("golden_diff", spec=str(spec), baseline=str(baseline),
                     cid="golden-tool-surface", run=True)
    assert tool_diff["status"] == "PASSED"
