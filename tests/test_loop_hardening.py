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
