import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.report import render
from ooptdd_loop.runner import run_loop
from ooptdd_loop.spec import load_spec
from ooptdd_loop.tools import call


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _write_selector_app(tmp_path):
    app = tmp_path / "selector_app.py"
    app.write_text(
        """
import time


def ev(cid, event, service, operation, **attrs):
    return {
        "cid": cid,
        "correlation_id": cid,
        "cycle_id": cid,
        "event": event,
        "service": service,
        "operation": operation,
        **attrs,
    }


def run_pipeline(backend, cid):
    backend.ship([ev(cid, "order_received", "web", "submit", channel="mobile")])
    time.sleep(0.001)
    backend.ship([ev(cid, "payment_authorized", "billing", "authorize", amount=42)])
    time.sleep(0.001)
    backend.ship([ev(cid, "payment_authorized", "fraud", "authorize", amount=42)])
    time.sleep(0.001)
    backend.ship([ev(cid, "order_shipped", "fulfillment", "ship", carrier="ups")])
""",
        encoding="utf-8",
    )


def _write_spec(tmp_path, gate: str, *, methodology: bool = False):
    prelude = """
methodology:
  name: OOPTDD_methodology_v1
  enforce: true
contracts:
  - id: MC-PAY
    kind: message_contract
    role: BillingService
    message: authorize
    status: accepted
    source_req: REQ-SELECT
    integration_backstop: REQ-SELECT
""" if methodology else ""
    spec = tmp_path / "requirements.yaml"
    spec.write_text(
        f"""
name: selector-demo
{prelude}
target:
  mode: in_process
  callable: selector_app:run_pipeline
  backend: memory
  root: {tmp_path}
requirements:
  - id: REQ-SELECT
    description: selector gate works
    kind: guiding
    covers: [MC-PAY]
    gate:
{gate}
    longinus:
      kg_anchor: ref_site:selector:payment
      source: selector_app.py
      symbol: run_pipeline
      must_emit: payment_authorized
""",
        encoding="utf-8",
    )
    return spec


def test_selector_count_gate_filters_event_service_operation_and_attrs(tmp_path):
    _write_selector_app(tmp_path)
    spec = _write_spec(
        tmp_path,
        """
      - select:
          event: payment_authorized
          service: billing
          operation: authorize
          attrs: {amount: 42}
        op: "=="
        count: 1
""",
    )

    run = run_loop(load_spec(str(spec)), cid="selector-count-green")

    assert run.complete
    check = run.results[0].checks[0]
    assert check["select"]["service"] == "billing"
    assert check["where"] == {"service": "billing", "operation": "authorize", "amount": 42}
    assert check["got"] == 1


def test_selector_order_and_causal_predecessor_gates(tmp_path):
    _write_selector_app(tmp_path)
    spec = _write_spec(
        tmp_path,
        """
      - must_order:
          - {event: order_received, service: web}
          - {event: payment_authorized, service: billing, operation: authorize}
          - {event: order_shipped, service: fulfillment}
      - select: {event: payment_authorized, service: billing}
        after: {event: order_received, service: web}
        within_s: 1
""",
    )

    run = run_loop(load_spec(str(spec)), cid="selector-order-green")

    assert run.complete
    assert run.results[0].checks[0]["passed"] is True
    assert run.results[0].checks[1]["passed"] is True


def test_selector_order_failure_renders_without_crashing(tmp_path):
    _write_selector_app(tmp_path)
    spec = _write_spec(
        tmp_path,
        """
      - must_order:
          - {event: order_shipped, service: fulfillment}
          - {event: order_received, service: web}
""",
    )

    run = run_loop(load_spec(str(spec)), cid="selector-order-red")

    assert not run.complete
    assert run.results[0].checks[0]["passed"] is False
    out = render(run)
    assert "selector order" in out


def test_ooptdd_methodology_accepts_selector_gate_shape(tmp_path):
    _write_selector_app(tmp_path)
    spec = _write_spec(
        tmp_path,
        """
      - select:
          event: payment_authorized
          service: billing
        op: ">="
        count: 1
""",
        methodology=True,
    )

    out = call("validate_spec", spec=str(spec))

    assert out["ok"] is True
    assert out["ooptdd_enabled"] is True
