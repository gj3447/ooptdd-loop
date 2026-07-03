"""run_until_complete is a real edit-run fixpoint loop, not a re-evaluator.

It invokes a fix command between RED passes (the agent edits code from the RCA), re-runs,
and stops on completion, a stall, or the pass budget — recording a per-pass transcript.
The fix command here is a tiny python script that writes the missing emitter into the
source, so the loop genuinely converges RED→GREEN across passes (which also exercises the
runner re-importing the edited target module each pass).
"""
from __future__ import annotations

import os
import sys

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.runner import run_until_complete
from ooptdd_loop.domain.spec import load_spec

GREEN_BODY = 'def run(backend, cid):\n    backend.ship([{"cid": cid, "event": "ping"}])\n'
RED_BODY = "def run(backend, cid):\n    pass\n"


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _modname(tmp_path) -> str:
    # unique per test so the import cache never serves another test's target module
    return "svc_" + os.path.basename(str(tmp_path)).replace("-", "_")


def _make(tmp_path, *, emits: bool) -> str:
    mod = _modname(tmp_path)
    (tmp_path / f"{mod}.py").write_text(GREEN_BODY if emits else RED_BODY, encoding="utf-8")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(f"""
target:
  mode: in_process
  callable: {mod}:run
  backend: memory
  root: {tmp_path}
requirements:
  - id: REQ-PING
    description: a ping is emitted
    gate: [{{event: ping, op: "==", count: 1}}]
    longinus: {{kg_anchor: 'ref:ping', source: {mod}.py, symbol: run, must_emit: ping}}
""")
    return str(spec_path)


def _fix_script(tmp_path) -> str:
    """A python 'agent': overwrite the target module with the emitting version."""
    mod = _modname(tmp_path)
    target = tmp_path / f"{mod}.py"
    fix_py = tmp_path / "fix.py"
    fix_py.write_text(
        f"open(r{str(target)!r}, 'w').write({GREEN_BODY!r})\n", encoding="utf-8")
    return f"{sys.executable} {fix_py}"


# ── single pass is fully backward compatible ───────────────────────────────────
def test_single_pass_red_is_not_complete(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path, emits=False)))
    assert not run.complete
    assert run.loop_reason == "single_pass"
    assert len(run.transcript) == 1 and run.transcript[0].red == ["REQ-PING"]


def test_single_pass_green_completes(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path, emits=True)))
    assert run.complete and run.loop_reason == "complete"


# ── the loop converges via a fix command that edits the code ───────────────────
def test_fix_command_converges_red_to_green(tmp_path):
    spec_path = _make(tmp_path, emits=False)             # starts RED (run() emits nothing)
    run = run_until_complete(load_spec(spec_path), max_passes=5,
                             fix_cmd=_fix_script(tmp_path))
    assert run.complete and run.loop_reason == "complete"
    assert run.transcript[0].fix_ran and run.transcript[0].fix_exit == 0
    assert run.transcript[-1].complete and len(run.transcript) == 2


def test_fix_runs_from_spec_target_fix(tmp_path):
    # the fix command can live in the spec (target.fix), not only the call site.
    mod = _modname(tmp_path)
    (tmp_path / f"{mod}.py").write_text(RED_BODY, encoding="utf-8")
    fix_cmd = _fix_script(tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(f"""
target:
  mode: in_process
  callable: {mod}:run
  backend: memory
  root: {tmp_path}
  fix: {fix_cmd!r}
requirements:
  - id: REQ-PING
    description: a ping is emitted
    gate: [{{event: ping, op: "==", count: 1}}]
    longinus: {{kg_anchor: 'ref:ping', source: {mod}.py, symbol: run, must_emit: ping}}
""")
    run = run_until_complete(load_spec(str(spec_path)), max_passes=4)
    assert run.complete and run.transcript[0].fix_ran


# ── a fix that never helps is detected as a stall (no infinite loop) ───────────
def test_stall_detected_when_fix_makes_no_progress(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path, emits=False)), max_passes=10,
                             fix_cmd="true", patience=2)   # `true` edits nothing
    assert not run.complete
    assert run.loop_reason == "stalled"
    assert len(run.transcript) < 10        # stopped early, did not spin the whole budget


def test_no_fix_multi_pass_stalls_without_spinning_budget(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path, emits=False)),
                             max_passes=10, patience=2)
    assert not run.complete and run.loop_reason == "stalled"


def test_fix_command_sees_rca_env(tmp_path):
    marker = tmp_path / "rca.txt"
    fix_cmd = f"printf '%s' \"$OOPTDD_RCA\" > {marker}"   # capture what the agent got
    run = run_until_complete(load_spec(_make(tmp_path, emits=False)),
                             max_passes=2, fix_cmd=fix_cmd, patience=5)
    assert not run.complete                       # printf doesn't fix the code
    assert os.path.exists(marker)
    assert "REQ-PING" in marker.read_text()       # the RCA named the RED requirement
