"""A raising target is a verdict, not a stack dump.

2026-08-06 sqcedit 실측에서 온 계약: 드라이버 단언이 죽으면 종전에는
``getattr(mod, fn)(backend, cid)`` 의 raw traceback 이 그대로 새어 나가
요구 판정이 한 줄도 출력되지 않았다 (cli 의 "never hand a user a raw
traceback" 약속 위반). 이제 크래시 전까지 도착한 이벤트로 전 요구를
평가하되, ``driver_error`` 가 ``complete`` 를 영구히 내린다 — 게이트가
초록이어도 크래시한 드라이버는 DONE 을 주장할 수 없다(vacuous-pass 방지).
"""
import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.report import render
from ooptdd_loop.runner import run_loop
from ooptdd_loop.domain.spec import load_spec


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _write_app(tmp_path, *, explode: bool):
    app = tmp_path / "driver_error_app.py"
    body = """
import logging

logger = logging.getLogger("checkout")


def run_pipeline(backend, cid):
    logger.info("paid", extra={"event": "paid", "operation": "pay"})
"""
    if explode:
        body += """    assert 1 == 2, "driver assert blew up"
"""
    app.write_text(body, encoding="utf-8")


def _write_spec(tmp_path):
    spec = tmp_path / "requirements_driver_error.yaml"
    spec.write_text(
        f"""
name: driver-error-demo
target:
  mode: in_process
  callable: driver_error_app:run_pipeline
  backend: memory
  root: {tmp_path}
  capture:
    logging: true
    logger: checkout
requirements:
  - id: REQ-PAID
    description: one payment event ships before the crash
    gate:
      - {{event: paid, op: "==", count: 1}}
""",
        encoding="utf-8",
    )
    return str(spec)


def test_raising_target_is_evaluated_and_never_complete(tmp_path):
    _write_app(tmp_path, explode=True)
    run = run_loop(load_spec(_write_spec(tmp_path)))

    # 크래시 전 이벤트는 실증거 — 게이트는 평가된다.
    assert [r.id for r in run.results] == ["REQ-PAID"]
    assert run.results[0].gate_ok
    # 그러나 크래시한 드라이버는 COMPLETE 를 주장할 수 없다.
    assert run.driver_error is not None
    assert "AssertionError" in run.driver_error
    assert "driver assert blew up" in run.driver_error
    # bare assert 계급의 외부 앵커(우로보로스 드라이버의 심볼 grep)가 살려면
    # 소스 라인이 실려야 한다 — 메시지 없는 assert 는 소스가 곧 사유다.
    assert "assert 1 == 2" in run.driver_error
    assert not run.complete

    out = render(run)
    assert "driver: FAILED" in out
    assert "INCOMPLETE" in out


def test_healthy_target_has_no_driver_error(tmp_path):
    _write_app(tmp_path, explode=False)
    run = run_loop(load_spec(_write_spec(tmp_path)))
    assert run.driver_error is None
    assert run.complete
    assert "driver: FAILED" not in render(run)


def test_import_failure_is_a_verdict_too(tmp_path):
    (tmp_path / "driver_error_app.py").write_text(
        "import does_not_exist_anywhere\n", encoding="utf-8")
    run = run_loop(load_spec(_write_spec(tmp_path)))
    assert run.driver_error is not None
    assert "does_not_exist_anywhere" in run.driver_error
    assert not run.complete
    assert not run.results[0].gate_ok      # 이벤트 0건 — 게이트도 정직하게 RED


# ── 호출 지점 (2026-08-07) ────────────────────────────────────────────────
#
# 최심 프레임 하나만 실으면 헬퍼를 부르는 드라이버에서 사유가 사라진다:
# 헬퍼의 소스 라인은 `assert observed == expected` 처럼 일반적이고, 값과
# 검사 이름을 든 줄은 **호출 지점**이다. 실측(sqcedit LX3 하네스)에서 그
# 한 줄만 보고는 어느 검사가 죽었는지 알 수 없었다.


def _write_helper_app(tmp_path, *, same_file: bool):
    """헬퍼가 드라이버와 같은 파일 / 다른 파일 — 두 모양 다 실제로 쓰인다."""
    helper = """
def check_angles(observed, expected):
    assert observed == expected
"""
    call = "    check_angles([0.0, -90.0], [0.0, -98.7])\n"
    head = """
import logging

logger = logging.getLogger("checkout")
"""
    body = head
    if same_file:
        body += helper
    else:
        (tmp_path / "angle_helper.py").write_text(helper, encoding="utf-8")
        body += "\nfrom angle_helper import check_angles\n"
    body += """

def run_pipeline(backend, cid):
    logger.info("paid", extra={"event": "paid", "operation": "pay"})
"""
    body += call
    (tmp_path / "driver_error_app.py").write_text(body, encoding="utf-8")


@pytest.mark.parametrize("same_file", [True, False])
def test_call_site_in_the_driver_is_named(tmp_path, same_file):
    _write_helper_app(tmp_path, same_file=same_file)
    run = run_loop(load_spec(_write_spec(tmp_path)))

    assert run.driver_error is not None
    # 터진 자리 — 헬퍼의 일반적인 assert.
    assert "assert observed == expected" in run.driver_error
    # ★ 그리고 사유 — 값을 든 드라이버의 호출 지점.
    assert "called from" in run.driver_error
    assert "check_angles([0.0, -90.0], [0.0, -98.7])" in run.driver_error
    assert "in run_pipeline" in run.driver_error
    assert not run.complete


def test_no_call_site_line_when_the_driver_itself_raises(tmp_path):
    """드라이버가 직접 터지면 호출 지점 줄은 없다 — 같은 프레임을 두 번
    쓰는 것은 정보가 아니라 소음이다."""
    _write_app(tmp_path, explode=True)
    run = run_loop(load_spec(_write_spec(tmp_path)))

    assert run.driver_error is not None
    assert "assert 1 == 2" in run.driver_error
    assert "called from" not in run.driver_error
