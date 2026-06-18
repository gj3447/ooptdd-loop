"""End-to-end: the loop turns 'did the agent satisfy R?' into a real verdict."""
import os

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.report import next_step_context, render
from ooptdd_loop.runner import run_loop
from ooptdd_loop.domain.spec import load_spec

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
MET = os.path.join(ROOT, "example", "requirements.yaml")
UNMET = os.path.join(ROOT, "example", "requirements_unmet.yaml")


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def test_satisfied_spec_is_complete():
    run = run_loop(load_spec(MET))
    assert run.complete
    assert run.n_done == 4
    assert all(r.gate_ok and r.bound for r in run.results)
    assert next_step_context(run) == ""  # nothing for the agent to do


def test_unmet_spec_is_incomplete_with_grounded_rca():
    run = run_loop(load_spec(UNMET))
    assert not run.complete

    fraud = next(r for r in run.results if r.id == "REQ-FRAUD")
    assert fraud.gate_ok is False             # event never emitted -> RED
    assert fraud.binding is not None and fraud.binding.bound is False  # symbol absent
    assert "MISSING" in (fraud.rca or "")     # log-grounded, names the gap

    received = next(r for r in run.results if r.id == "REQ-1")
    assert received.done                       # the already-satisfied one stays GREEN

    ctx = next_step_context(run)
    assert "REQ-FRAUD" in ctx and "do not edit the spec" in ctx


def test_longinus_unbound_when_symbol_missing():
    run = run_loop(load_spec(UNMET))
    fraud = next(r for r in run.results if r.id == "REQ-FRAUD")
    assert "run_fraud_check" in fraud.binding.reason


def test_render_smoke():
    out = render(run_loop(load_spec(MET)))
    assert "COMPLETE" in out and "REQ-1" in out


ONTO_SPEC = os.path.join(ROOT, "example", "requirements_ontology.yaml")


def test_ontology_requirements_complete():
    # shop emits a conforming payment_authorized (with amount) -> conforms GREEN
    run = run_loop(load_spec(ONTO_SPEC))
    assert run.complete, [(_r.id, _r.checks) for _r in run.results]


def test_conforms_violation_is_red_and_renders(tmp_path):
    # an ontology requiring an attr the code never emits -> conforms RED, no crash
    (tmp_path / "onto.yaml").write_text(
        "event_types:\n  order_shipped:\n    required: [tracking_no]\n"
    )
    spec_txt = """
target:
  mode: in_process
  callable: shop:run_pipeline
  backend: memory
  root: %s
  ontology: %s
requirements:
  - id: SHIP-CONFORMS
    description: shipment carries a tracking number
    gate: [{conforms: order_shipped}]
""" % (os.path.join(ROOT, "example"), str(tmp_path / "onto.yaml"))
    p = tmp_path / "spec.yaml"
    p.write_text(spec_txt)
    run = run_loop(load_spec(str(p)))
    r = run.results[0]
    assert r.gate_ok is False and not run.complete
    out = render(run)                       # must not raise on a conforms failure
    assert "conforms" in out and "tracking_no" in out


def test_must_order_and_where_gates(tmp_path):
    # richer ooptdd gate shapes flow through the loop without crashing, both GREEN
    # (correct order / matching where) and RED (impossible order) — regression for
    # the KeyError('event') the loop hit on a failing must_order check.
    spec_txt = """
target: {mode: in_process, callable: shop:run_pipeline, backend: memory, root: %s}
requirements:
  - id: OK-ORDER
    description: steps in order
    gate: [{must_order: [order_received, payment_authorized, order_shipped]}]
  - id: OK-WHERE
    description: 3 items
    gate: [{event: order_received, where: {items: 3}, op: "==", count: 1}]
  - id: BAD-ORDER
    description: impossible order
    gate: [{must_order: [order_shipped, order_received]}]
""" % os.path.join(ROOT, "example")
    p = tmp_path / "spec.yaml"
    p.write_text(spec_txt)
    run = run_loop(load_spec(str(p)))
    by = {r.id: r for r in run.results}
    assert by["OK-ORDER"].gate_ok and by["OK-WHERE"].gate_ok
    assert by["BAD-ORDER"].gate_ok is False
    out = render(run)                       # must not raise on the failed must_order
    assert "out of order" in out
    assert not run.complete
