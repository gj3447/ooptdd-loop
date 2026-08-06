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


# ── 방출 순서 정규화 (2026-08-07) ─────────────────────────────────────────

_TWO = [
    'backend.ship([ev(cid, "order_received", amount=42)])',
    'backend.ship([ev(cid, "payment_authorized", amount=42)])',
]
_TWO_SWAPPED = [
    'backend.ship([ev(cid, "payment_authorized", amount=42)])',
    'backend.ship([ev(cid, "order_received", amount=42)])',
]


def test_identical_trace_matches_itself_across_runs(tmp_path):
    """골든이 자기 자신과 맞아야 한다 — 계측기의 최소 조건.

    `_emit_seq` 는 프로세스 전역 카운터라 두 번째 실행이 같은 트레이스에
    다른 번호를 찍는다. 그대로 비교하면 골든은 **영원히** OUTPUT_CHANGED 다
    (실측 2026-08-07). 늘 우는 게이트는 곧 무시되는 게이트다.
    """
    spec = _scenario(tmp_path, "golden_selfmatch", _TWO)
    baseline = tmp_path / "golden_self.json"
    save_golden(load_spec(str(spec)), out=str(baseline), cid="golden-a", run=True)

    for cid in ("golden-b", "golden-c"):        # 오프셋이 계속 커져도 무관해야
        diff = diff_golden(load_spec(str(spec)), baseline=str(baseline),
                           cid=cid, run=True)
        assert diff["status"] == "PASSED", (cid, diff["changes"])


def test_reordering_is_still_caught_after_normalization(tmp_path):
    """★ 정규화가 눈을 멀게 하지 않았는가 — 순서를 바꾸면 붉어야 한다.

    절대 오프셋을 지우면서 상대 순서까지 지웠다면 이 시험이 통과하지 않는다.
    (모듈 이름을 갈라 쓰는 이유: 같은 이름에 같은 크기로 덮어쓰면 .pyc 가
    같은 mtime/size 로 재사용돼 **바꾼 소스가 안 돈다** — 2026-08-07 실측.
    형제 시험들이 시나리오마다 새 이름을 쓰는 것도 같은 이유다.)
    """
    base = _scenario(tmp_path, "golden_order_base", _TWO)
    swapped = _scenario(tmp_path, "golden_order_swapped", _TWO_SWAPPED)
    baseline = tmp_path / "golden_order.json"
    save_golden(load_spec(str(base)), out=str(baseline), cid="golden-a", run=True)

    diff = diff_golden(load_spec(str(swapped)), baseline=str(baseline),
                       cid="golden-swapped", run=True)
    assert diff["status"] != "PASSED", (
        "방출 순서가 뒤집혔는데 골든이 통과했다 — 정규화가 순서까지 지웠다")
    assert any(c["kind"] == "event_identity_sequence" for c in diff["changes"])
