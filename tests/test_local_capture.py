import time

import pytest

from ooptdd.backends import get_backend, memory_reset
from ooptdd_loop.local_capture import structlog_event_processor
from ooptdd_loop.runner import run_loop
from ooptdd_loop.domain.spec import load_spec


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _write_app(tmp_path):
    app = tmp_path / "capture_app.py"
    app.write_text(
        """
import logging


logger = logging.getLogger("checkout")


def run_pipeline(backend, cid):
    logger.info(
        "payment authorized",
        extra={
            "event": "payment_authorized",
            "operation": "authorize",
            "amount": 42,
        },
    )
    logger.info({
        "event": "order_shipped",
        "service": "fulfillment",
        "operation": "ship",
        "tracking_no": "T-1",
    })
""",
        encoding="utf-8",
    )


def _write_spec(tmp_path):
    spec = tmp_path / "requirements_capture.yaml"
    spec.write_text(
        f"""
name: local-capture-demo
target:
  mode: in_process
  callable: capture_app:run_pipeline
  backend: memory
  root: {tmp_path}
  capture:
    logging: true
    logger: checkout
requirements:
  - id: REQ-PAY
    description: payment log is captured as an event
    gate:
      - select:
          event: payment_authorized
          service: checkout
          operation: authorize
          attrs: {{amount: 42}}
        op: "=="
        count: 1
    longinus:
      kg_anchor: ref_site:capture:payment
      source: capture_app.py
      symbol: run_pipeline
      must_emit: payment_authorized
  - id: REQ-SHIP
    description: dict log message is captured as an event
    gate:
      - select:
          event: order_shipped
          service: fulfillment
          operation: ship
          attrs: {{tracking_no: T-1}}
        op: "=="
        count: 1
    longinus:
      kg_anchor: ref_site:capture:ship
      source: capture_app.py
      symbol: run_pipeline
      must_emit: order_shipped
""",
        encoding="utf-8",
    )
    return spec


def test_runner_captures_structured_logging_into_backend(tmp_path):
    _write_app(tmp_path)
    spec = _write_spec(tmp_path)

    run = run_loop(load_spec(str(spec)), cid="local-capture-green")

    assert run.complete
    assert run.n_done == 2
    by_id = {r.id: r for r in run.results}
    assert by_id["REQ-PAY"].checks[0]["got"] == 1
    assert by_id["REQ-SHIP"].checks[0]["got"] == 1


def test_structlog_processor_ships_event_dict_without_structlog_dependency():
    backend = get_backend("memory")
    processor = structlog_event_processor(backend, "structlog-cid", service="billing")
    event_dict = {"event": "payment_authorized", "amount": 42}

    returned = processor(None, "info", event_dict)

    assert returned is event_dict
    events = backend.query("structlog-cid", since_us=0, until_us=int(time.time() * 1_000_000) + 1)
    assert events.events[0]["event"] == "payment_authorized"
    assert events.events[0]["service"] == "billing"
    assert events.events[0]["level"] == "info"


def test_capture_accepts_sibling_logger_list(tmp_path):
    """한 스펙이 계층을 가로지르는 파이프라인(엔진 로거+어댑터 로거)을 한 번에 잡는다.

    2026-08-06 sqcedit 실측 요구: 엔진 완주(terminal)는 'sqcedit', 뷰 보존은
    'lx3harness' 로거라 단일 캡처로는 두 축을 한 판정에 못 물었다. service 는
    강제하지 않으면 레코드의 로거명 — 게이트가 축별로 갈린다.
    """
    import logging

    from ooptdd.backends import get_backend
    from ooptdd_loop.local_capture import capture_logging_to_backend

    backend = get_backend("memory")
    with capture_logging_to_backend(backend, "cid-multi",
                                    logger_name=["axis_a", "axis_b"]) as handler:
        logging.getLogger("axis_a").info(
            "a", extra={"event": "engine_done", "operation": "run"})
        logging.getLogger("axis_b").info(
            "b", extra={"event": "view_kept", "operation": "capture"})
        logging.getLogger("axis_unrelated").info(
            "c", extra={"event": "noise", "operation": "x"})

    got = {(e["event"], e.get("service")) for e in handler.events}
    assert got == {("engine_done", "axis_a"), ("view_kept", "axis_b")}

    # 핸들러가 양쪽 로거에서 대칭으로 제거됐다 — 캡처 밖 방출은 잡히지 않는다.
    logging.getLogger("axis_a").info(
        "late", extra={"event": "late", "operation": "run"})
    assert {e["event"] for e in handler.events} == {"engine_done", "view_kept"}
