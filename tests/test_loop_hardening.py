"""Regressions for two false-green / evidence-corruption holes the audit found.

1. An empty gate (`gate: []`, or a YAML typo that mis-nests the expectations) asserted
   nothing yet reported GREEN/DONE — a requirement proving nothing was "done".
2. ``run_until_complete`` re-ran the system under test on *every* pass, re-shipping every
   event. With a stable cid on an in-process store that doubled the counts and flipped an
   exact-count gate (``op: ==``) from GREEN to RED against correct code.
"""
from __future__ import annotations

from ooptdd.backends import MemoryBackend, memory_reset

from ooptdd_loop import runner
from ooptdd_loop.selector_gates import evaluate_gate
from ooptdd_loop.spec import Requirement, Spec, Target


def test_empty_gate_asserts_nothing_and_is_not_green():
    memory_reset()
    res = evaluate_gate(MemoryBackend(), {"cid": "c", "expect": []})
    assert res["checks"] == []      # nothing was evaluated...
    assert res["empty"] is True
    assert res["ok"] is False       # ...so it is NOT a clean pass (was True: vacuous all([]))


def test_run_until_complete_runs_sut_once_no_double_count(monkeypatch):
    # REQ-1 wants exactly one `paid`; REQ-2 wants an event that never ships, so the run is
    # never complete and a 2nd pass fires. The old code re-produced logs on that 2nd pass,
    # doubling REQ-1's count to 2 and flipping its `== 1` gate RED. The SUT must run once.
    memory_reset()
    calls = {"n": 0}

    def fake_produce(spec, backend, cid):
        calls["n"] += 1
        backend.ship([{"cid": cid, "event": "paid"}])

    monkeypatch.setattr(runner, "_produce_logs", fake_produce)

    spec = Spec(
        target=Target(mode="in_process", callable="x:y", backend="memory", root="."),
        requirements=[
            Requirement(id="REQ-1", description="paid exactly once",
                        gate=[{"event": "paid", "op": "==", "count": 1}]),
            Requirement(id="REQ-2", description="forces a 2nd pass (never satisfied)",
                        gate=[{"event": "never", "op": ">=", "count": 1}]),
        ],
    )
    run = runner.run_until_complete(spec, cid="fixed-cid", max_passes=2)

    assert calls["n"] == 1  # the system under test is produced once, not once per pass
    req1 = next(r for r in run.results if r.id == "REQ-1")
    assert req1.gate_ok is True  # count stays 1 — re-passes re-query, they do not re-ship


# ── PROM07 incentive-loop hardening: require-binding + mutation-gate ───────────
def _spec(reqs):
    return Spec(target=Target(mode="in_process", callable="x:y", backend="memory", root="."),
                requirements=reqs)


def _ship_and_eval(cid, reqs, monkeypatch=None, env=None):
    memory_reset()
    b = MemoryBackend()
    b.ship([{"cid": cid, "event": "paid", "_timestamp": 1}])
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return runner.evaluate_requirements(_spec(reqs), cid=cid, backend=b)


def test_require_binding_inverts_the_missing_binding_default(monkeypatch):
    req = [Requirement(id="R1", description="paid, no binding",
                       gate=[{"event": "paid", "op": ">=", "count": 1}])]
    # default (not enforced): a missing binding is bound -> done
    assert _ship_and_eval("rb1", req).results[0].done is True
    # enforced: a missing binding is NOT bound -> not done (closes done-by-omission)
    r = _ship_and_eval("rb2", req, monkeypatch, {"OOPTDD_REQUIRE_BINDING": "1"}).results[0]
    assert r.gate_ok is True and r.bound is False and r.done is False


def test_binding_waiver_acknowledges_a_missing_binding_under_enforcement(monkeypatch):
    req = [Requirement(id="R1", description="paid, waived", extras={"binding_waiver": "no src yet"},
                       gate=[{"event": "paid", "op": ">=", "count": 1}])]
    r = _ship_and_eval("rb3", req, monkeypatch, {"OOPTDD_REQUIRE_BINDING": "1"}).results[0]
    assert r.bound is True and r.done is True  # an explicit waiver re-allows it


def test_mutation_gate_blocks_a_gate_below_the_floor():
    # the gating logic is deterministic at the property level (the score itself is mutation.py's)
    r_weak = runner.ReqResult("R", "", True, True, [], None, min_mutation_score=0.8, mutation_score=0.5)
    assert r_weak.mutation_ok is False and r_weak.done is False
    r_strong = runner.ReqResult("R", "", True, True, [], None, min_mutation_score=0.8, mutation_score=0.9)
    assert r_strong.mutation_ok is True and r_strong.done is True
    r_nobaseline = runner.ReqResult("R", "", True, True, [], None, min_mutation_score=0.8, mutation_score=None)
    assert r_nobaseline.mutation_ok is True  # no baseline -> not blocked


def test_mutation_gate_integration_populates_score_without_crashing(monkeypatch):
    monkeypatch.setenv("OOPTDD_MIN_MUTATION_SCORE", "0.1")
    req = [Requirement(id="R1", description="paid",
                       gate=[{"event": "paid", "where": {"_timestamp": 1}, "op": ">=", "count": 1}])]
    r = _ship_and_eval("mut1", req, monkeypatch, {"OOPTDD_MIN_MUTATION_SCORE": "0.1"}).results[0]
    assert r.mutation_score is None or isinstance(r.mutation_score, float)  # populated or skipped
