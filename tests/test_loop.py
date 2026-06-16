"""End-to-end: the loop turns 'did the agent satisfy R?' into a real verdict."""
import os

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.report import next_step_context, render
from ooptdd_loop.runner import run_loop
from ooptdd_loop.spec import load_spec

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
