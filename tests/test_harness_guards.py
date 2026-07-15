"""The loop's independent stop controls, durable state, and fix containment.

``run_until_complete`` used to be bounded by a pass count and a stall detector only. That
leaves four holes, and this file is one section per hole — each proving BOTH directions:
the guard fires when it should, and the normal path is untouched.

* S5 wall-clock / spend  — a pass budget cannot see an hour or $40 burned inside one pass.
* S5 fix timeout         — a fix that never returns makes the between-pass checks unreachable.
* S4 journal / resume    — an in-memory transcript means a crash restarts at pass 1 and
                           repays every agent call.
* S7 containment         — the fix ran with the full parent env and could write anywhere.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop import harness
from ooptdd_loop.harness import (
    DurableRunJournal,
    INHERIT_ALL,
    JournalCorruptionError,
    JournalEntry,
    LoopGuard,
    LoopReason,
    audit_writeset,
    fix_env,
    git_head,
    spend_file_reader,
)
from ooptdd_loop.runner import run_until_complete
from ooptdd_loop.domain.spec import load_spec

GREEN_BODY = 'def run(backend, cid):\n    backend.ship([{"cid": cid, "event": "ping"}])\n'
RED_BODY = "def run(backend, cid):\n    pass\n"
SLOW_RED_BODY = "import time\n\n\ndef run(backend, cid):\n    time.sleep(0.2)\n"


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _modname(tmp_path) -> str:
    # unique per test so the import cache never serves another test's target module
    return "svc_" + os.path.basename(str(tmp_path)).replace("-", "_")


def _make(tmp_path, *, body: str = RED_BODY, root=None) -> str:
    """A one-requirement spec whose target module is `body`. RED unless it ships `ping`."""
    root = root or tmp_path
    mod = _modname(tmp_path)
    (root / f"{mod}.py").write_text(body, encoding="utf-8")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(f"""
target:
  mode: in_process
  callable: {mod}:run
  backend: memory
  root: {root}
requirements:
  - id: REQ-PING
    description: a ping is emitted
    gate: [{{event: ping, op: "==", count: 1}}]
    longinus: {{kg_anchor: 'ref:ping', source: {mod}.py, symbol: run, must_emit: ping}}
""")
    return str(spec_path)


def _fix_script(tmp_path, root=None) -> str:
    """A python 'agent': overwrite the target module with the emitting version."""
    target = (root or tmp_path) / f"{_modname(tmp_path)}.py"
    fix_py = tmp_path / "fix.py"
    fix_py.write_text(f"open(r{str(target)!r}, 'w').write({GREEN_BODY!r})\n", encoding="utf-8")
    return f"{sys.executable} {fix_py}"


def _sleep_fix(seconds: float) -> str:
    return f"{sys.executable} -c 'import time; time.sleep({seconds})'"


# ── LoopGuard: the unit-level contract ────────────────────────────────────────
def test_max_spend_without_spend_fn_is_a_config_error_not_a_silent_no_op():
    # the whole point: a budget with no meter would never fire, and the caller would
    # believe they were protected.
    with pytest.raises(ValueError, match="spend_fn"):
        LoopGuard(max_spend=10.0)


def test_max_spend_with_a_meter_constructs():
    guard = LoopGuard(max_spend=10.0, spend_fn=lambda: 0.0)
    assert guard.resource_stop() is None


def test_guard_reasons_are_typed_but_serialize_as_the_existing_vocabulary():
    assert LoopReason.STALLED == "stalled"
    assert json.dumps({"r": LoopReason.BUDGET_TIME}) == '{"r": "budget_time"}'
    assert f"{LoopReason.FIX_TIMEOUT}" == "fix_timeout"


def test_wall_clock_stop_fires_on_an_injected_clock():
    now = {"t": 0.0}
    guard = LoopGuard(max_seconds=1.0, clock=lambda: now["t"])
    guard.start()
    assert guard.resource_stop() is None       # nothing spent yet
    now["t"] = 5.0
    assert guard.resource_stop() == LoopReason.BUDGET_TIME
    assert "wall-clock" in guard.stop_note


def test_fix_timeout_is_the_tighter_of_the_knob_and_the_remaining_wall_clock():
    assert LoopGuard().fix_timeout() is None                      # unbudgeted stays unbounded
    assert LoopGuard(fix_timeout_s=5.0).fix_timeout() == 5.0
    bounded = LoopGuard(max_seconds=100.0, fix_timeout_s=5.0)
    assert bounded.fix_timeout() == 5.0                           # the knob is tighter
    clamped = LoopGuard(max_seconds=2.0, fix_timeout_s=60.0)
    assert 0 < clamped.fix_timeout() <= 2.0                       # the wall-clock is tighter


def test_an_unreadable_spend_meter_stops_the_loop_fail_closed():
    def broken() -> float:
        raise RuntimeError("cost API down")

    guard = LoopGuard(max_spend=1.0, spend_fn=broken)
    assert guard.resource_stop() == LoopReason.BUDGET_SPEND   # not None: it must BLOCK
    assert "fail-closed" in guard.stop_note


def test_spend_file_reader_reads_the_meter_and_refuses_to_guess(tmp_path):
    meter = tmp_path / "spend.txt"
    read = spend_file_reader(meter)
    assert read() == 0.0            # not started yet is genuinely zero
    meter.write_text("12.5", encoding="utf-8")
    assert read() == 12.5
    meter.write_text("not a number", encoding="utf-8")
    with pytest.raises(ValueError):  # LoopGuard turns this into a fail-closed stop
        read()


def test_step_ceiling_precedes_the_stall_and_only_later_passes_can_stall():
    guard = LoopGuard(max_passes=3, patience=2)
    guard.note_progress(False, pass_no=1)     # the first pass has nothing to be identical to
    assert guard.stall == 0
    guard.note_progress(False, pass_no=2)
    guard.note_progress(False, pass_no=3)
    assert guard.stalled
    assert guard.step_stop(3) == LoopReason.MAX_PASSES   # the ceiling wins at the ceiling
    assert guard.step_stop(2) == LoopReason.STALLED


# ── GAP-1 (S5): wall-clock + spend kill-switch, through the runner ────────────
def test_wall_clock_budget_stops_a_loop_the_pass_budget_would_let_run(tmp_path):
    # the SUT sleeps 0.2s per pass, so a 0.05s budget is spent by the end of pass 1 even
    # though passes 2..5 are still "affordable" by pass count.
    run = run_until_complete(load_spec(_make(tmp_path, body=SLOW_RED_BODY)),
                             max_passes=5, fix_cmd="true", patience=5, max_seconds=0.05)
    assert run.loop_reason == LoopReason.BUDGET_TIME
    assert not run.complete
    assert len(run.transcript) == 1          # it stopped instead of spending the budget
    assert "wall-clock" in run.loop_note


def test_a_generous_wall_clock_budget_leaves_the_normal_path_alone(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=5,
                             fix_cmd=_fix_script(tmp_path), max_seconds=60)
    assert run.complete and run.loop_reason == LoopReason.COMPLETE


def test_spend_budget_stops_the_loop_before_paying_the_agent_again(tmp_path):
    calls = {"n": 0}

    def meter() -> float:
        calls["n"] += 1
        return 9.99          # the agent already burned the budget inside pass 1

    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=5, fix_cmd="true",
                             patience=5, max_spend=1.0, spend_fn=meter)
    assert run.loop_reason == LoopReason.BUDGET_SPEND
    assert len(run.transcript) == 1
    assert calls["n"] >= 1                   # the meter was actually consulted
    assert "spend budget spent" in run.loop_note


def test_spend_under_budget_does_not_disturb_the_loop(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=5,
                             fix_cmd=_fix_script(tmp_path),
                             max_spend=100.0, spend_fn=lambda: 0.01)
    assert run.complete and run.loop_reason == LoopReason.COMPLETE


def test_runner_rejects_a_spend_budget_with_no_meter(tmp_path):
    with pytest.raises(ValueError, match="spend_fn"):
        run_until_complete(load_spec(_make(tmp_path)), max_spend=5.0)


# ── GAP-1b (S5): a hung fix cannot outlive its bound ─────────────────────────
def test_fix_timeout_kills_a_hung_fix_and_stops_with_a_typed_reason(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=3,
                             fix_cmd=_sleep_fix(30), patience=5, fix_timeout_s=0.3)
    assert run.loop_reason == LoopReason.FIX_TIMEOUT     # not a TimeoutExpired traceback
    assert not run.complete
    assert run.transcript[-1].fix_ran and run.transcript[-1].fix_timed_out
    assert "killed" in run.loop_note


def test_the_wall_clock_budget_alone_bounds_a_hung_fix(tmp_path):
    # no fix_timeout_s at all: the remaining wall-clock must still bound the subprocess,
    # or a fix that never returns makes every between-pass check unreachable.
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=3,
                             fix_cmd=_sleep_fix(30), patience=5, max_seconds=0.5)
    assert run.loop_reason == LoopReason.FIX_TIMEOUT
    assert run.transcript[-1].fix_timed_out


def test_a_fix_that_finishes_inside_its_timeout_is_unaffected(tmp_path):
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=3,
                             fix_cmd=_fix_script(tmp_path), fix_timeout_s=30)
    assert run.complete and run.loop_reason == LoopReason.COMPLETE
    assert not run.transcript[0].fix_timed_out


# ── GAP-2 (S4): the journal, and resuming without repaying ───────────────────
def _counting_fix(tmp_path) -> tuple[str, object]:
    """A 'agent' that never fixes anything but records every time it was paid for."""
    counter = tmp_path / "calls.txt"
    script = tmp_path / "count.py"
    script.write_text(
        f"import pathlib; p = pathlib.Path(r{str(counter)!r}); "
        "p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}", counter


def test_journal_appends_one_line_per_completed_pass(tmp_path):
    journal = tmp_path / "run.jsonl"
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=2, fix_cmd="true",
                             patience=5, journal_path=journal, run_id="RUN-A")
    assert run.loop_reason == LoopReason.MAX_PASSES
    rows = [json.loads(x) for x in journal.read_text().splitlines()]
    assert [r["pass_no"] for r in rows] == [1, 2]
    assert {r["run_id"] for r in rows} == {"RUN-A"}
    assert rows[0]["red"] == ["REQ-PING"] and rows[0]["complete"] is False
    # the gate is RED and the binding is UNBOUND too — RED_BODY never mentions `ping`
    assert rows[0]["gates"] == [{"id": "REQ-PING", "gate_ok": False, "bound": False,
                                 "done": False}]
    assert rows[1]["stall"] == 1                      # pass 2 changed nothing
    assert rows[1]["ended_at"] >= rows[1]["started_at"]


def test_resume_starts_at_the_next_unpaid_pass_instead_of_repaying_the_agent(tmp_path):
    journal = tmp_path / "run.jsonl"
    fix_cmd, counter = _counting_fix(tmp_path)
    spec = load_spec(_make(tmp_path))

    first = run_until_complete(spec, max_passes=2, fix_cmd=fix_cmd, patience=5,
                               journal_path=journal, run_id="RUN-B")
    assert first.loop_reason == LoopReason.MAX_PASSES
    assert int(counter.read_text()) == 1              # paid the agent once

    # the process dies here; a new one resumes the same run_id with a bigger budget.
    resumed = run_until_complete(spec, max_passes=4, fix_cmd=fix_cmd, patience=5,
                                 journal_path=journal, run_id="RUN-B", resume=True)
    assert resumed.loop_reason == LoopReason.MAX_PASSES
    assert [p.pass_no for p in resumed.transcript] == [1, 2, 3, 4]   # the whole run
    assert [p.resumed for p in resumed.transcript] == [True, True, False, False]
    assert int(counter.read_text()) == 2             # passes 1-2 were NOT repaid...

    counter.unlink()
    fresh = run_until_complete(spec, max_passes=4, fix_cmd=fix_cmd, patience=5)
    assert int(counter.read_text()) == 3             # ...which a fresh 4-pass run would be
    assert fresh.loop_reason == resumed.loop_reason  # and the verdict is the same either way
    assert fresh.n_done == resumed.n_done


def test_resume_carries_the_stall_state_across_the_crash(tmp_path):
    # pass 2 already made no progress (stall=1) before the crash. A resume that forgot it
    # would need two MORE dead passes to notice the agent is going in circles.
    journal = tmp_path / "run.jsonl"
    spec = load_spec(_make(tmp_path))
    run_until_complete(spec, max_passes=2, fix_cmd="true", patience=2,
                       journal_path=journal, run_id="RUN-C")
    resumed = run_until_complete(spec, max_passes=9, fix_cmd="true", patience=2,
                                 journal_path=journal, run_id="RUN-C", resume=True)
    assert resumed.loop_reason == LoopReason.STALLED
    assert len(resumed.transcript) == 3        # one more pass was enough to trip patience


def test_resume_without_a_journal_is_a_config_error(tmp_path):
    with pytest.raises(ValueError, match="journal_path"):
        run_until_complete(load_spec(_make(tmp_path)), resume=True)


def test_a_journal_only_replays_its_own_run_id(tmp_path):
    journal = tmp_path / "shared.jsonl"
    spec = load_spec(_make(tmp_path))
    run_until_complete(spec, max_passes=2, fix_cmd="true", patience=5,
                       journal_path=journal, run_id="OTHER")
    assert DurableRunJournal(journal, "MINE").replay().passes == 0
    assert DurableRunJournal(journal, "OTHER").replay().passes == 2


def test_replay_drops_a_torn_last_line_but_rejects_real_corruption(tmp_path):
    journal = tmp_path / "j.jsonl"
    entry = JournalEntry(run_id="R", pass_no=1, cid="c", complete=False, n_done=0, total=1,
                         red=("REQ-1",), gates=(), progressed=True, stall=0, state_key="k",
                         started_at=1.0, ended_at=2.0)
    j = DurableRunJournal(journal, "R")
    j.append(entry)

    with open(journal, "a", encoding="utf-8") as fh:
        fh.write('{"run_id": "R", "pass_no": 2, "ci')     # a crash mid-append
    assert j.replay().passes == 1                          # the torn tail is not corruption

    journal.write_text('{"run_id": "R", "pass_no": 1}\n{"nope": 1}\n', encoding="utf-8")
    with pytest.raises(JournalCorruptionError):            # a malformed *interior* entry is
        j.replay()                                         # a bug — fail fast, never guess


# ── FIX-A regression: the resume-past-budget fallback must MEASURE ───────────
def test_resume_past_budget_measures_green_code_instead_of_fabricating_red(tmp_path):
    # A crash can land after the agent's edit but before the loop re-measured it. Resuming
    # at/after the budget runs no pass at all, so the fallback verdict is the whole answer:
    # re-querying an unproduced cid would return zero events and report an all-RED verdict
    # for code that is GREEN on disk — a fabricated verdict, not a measured one.
    journal = tmp_path / "run.jsonl"
    spec_path = _make(tmp_path)                    # starts RED
    spec = load_spec(spec_path)
    first = run_until_complete(spec, max_passes=1, fix_cmd="true",
                               journal_path=journal, run_id="RUN-D")
    assert not first.complete and first.loop_reason == LoopReason.SINGLE_PASS

    # the agent's edit landed; the process died before the loop could re-run.
    (tmp_path / f"{_modname(tmp_path)}.py").write_text(GREEN_BODY, encoding="utf-8")

    resumed = run_until_complete(spec, max_passes=1, fix_cmd="true",
                                 journal_path=journal, run_id="RUN-D", resume=True)
    assert resumed.complete is True                 # measured, not manufactured
    assert resumed.n_done == 1 and resumed.results[0].gate_ok is True
    assert resumed.loop_reason == LoopReason.COMPLETE


def test_resume_past_budget_still_reports_red_code_as_red(tmp_path):
    # the other direction: the fallback is a real measurement, so genuinely RED code on
    # disk must still come back RED (not accidentally green-washed).
    journal = tmp_path / "run.jsonl"
    spec = load_spec(_make(tmp_path))
    run_until_complete(spec, max_passes=1, fix_cmd="true", journal_path=journal,
                       run_id="RUN-E")
    resumed = run_until_complete(spec, max_passes=1, fix_cmd="true", journal_path=journal,
                                 run_id="RUN-E", resume=True)
    assert not resumed.complete and resumed.n_done == 0
    assert resumed.loop_reason == LoopReason.SINGLE_PASS
    assert "resumed past" in resumed.loop_note


# ── GAP-3 (S7): env scrub ────────────────────────────────────────────────────
def test_fix_env_scrubs_everything_not_on_the_allowlist():
    env = fix_env({"OOPTDD_CID": "c"},
                  environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-secret", "AWS_SECRET": "x"})
    assert env["PATH"] == "/bin" and env["OOPTDD_CID"] == "c"
    assert "ANTHROPIC_API_KEY" not in env and "AWS_SECRET" not in env


def test_fix_env_passes_an_explicitly_allowed_credential():
    env = fix_env({}, allowlist=[*harness.DEFAULT_ENV_ALLOWLIST, "ANTHROPIC_API_KEY"],
                  environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-1", "OPENAI_API_KEY": "sk-2"})
    assert env["ANTHROPIC_API_KEY"] == "sk-1"     # the migration path for a real agent fix
    assert "OPENAI_API_KEY" not in env            # and only what was asked for


def test_inherit_all_sentinel_restores_the_pre_scrub_behavior():
    parent = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-1", "WHATEVER": "y"}
    assert fix_env({}, allowlist=INHERIT_ALL, environ=parent) == parent
    assert fix_env({}, allowlist=[INHERIT_ALL], environ=parent) == parent   # list form too


def test_injected_ooptdd_vars_always_win_over_the_parent_env():
    env = fix_env({"OOPTDD_CID": "real"}, allowlist=INHERIT_ALL,
                  environ={"OOPTDD_CID": "stale"})
    assert env["OOPTDD_CID"] == "real"   # the loop's contract, not the caller's leftovers


def _env_dump_fix(tmp_path):
    out = tmp_path / "env.json"
    script = tmp_path / "dump.py"
    script.write_text(
        f"import json, os, pathlib; "
        f"pathlib.Path(r{str(out)!r}).write_text(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}", out


def test_the_loop_scrubs_the_real_fix_subprocess_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    fix_cmd, out = _env_dump_fix(tmp_path)
    run_until_complete(load_spec(_make(tmp_path)), max_passes=2, fix_cmd=fix_cmd, patience=5)
    env = json.loads(out.read_text())
    assert "ANTHROPIC_API_KEY" not in env          # the guard is wired to the real loop
    assert env["OOPTDD_CID"] and env["OOPTDD_RCA"]  # and the fix still gets its evidence


def test_the_loop_honors_the_inherit_all_sentinel_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-opted-in")
    fix_cmd, out = _env_dump_fix(tmp_path)
    run_until_complete(load_spec(_make(tmp_path)), max_passes=2, fix_cmd=fix_cmd, patience=5,
                       env_allowlist=INHERIT_ALL)
    assert json.loads(out.read_text())["ANTHROPIC_API_KEY"] == "sk-opted-in"


# ── GAP-3 (S7): write-set confinement ────────────────────────────────────────
def _repo(tmp_path):
    """A git work tree to audit. The SUT's own __pycache__/ lands here too — which is why
    the allowlist below must name it (see harness.audit_writeset: --ignored=matching makes
    the audited write-set deliberately over-inclusive)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-qm", "seed"], check=True)
    return root


def test_a_fix_writing_outside_its_declared_paths_stops_the_loop(tmp_path):
    root = _repo(tmp_path)
    spec = load_spec(_make(tmp_path, root=root))
    escape = root / "elsewhere" / "sneaky.txt"
    fix_cmd = (f"{sys.executable} -c \"import pathlib; p = pathlib.Path(r'{escape}'); "
               "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('x')\"")
    run = run_until_complete(spec, max_passes=3, fix_cmd=fix_cmd, patience=5,
                             write_allowlist=[str(root / f"{_modname(tmp_path)}.py"),
                                              str(root / "__pycache__")])
    assert run.loop_reason == LoopReason.WRITESET_VIOLATION
    assert any("sneaky.txt" in p for p in run.transcript[-1].writeset_outside)
    assert "outside its declared paths" in run.loop_note


def test_a_fix_writing_only_inside_its_declared_paths_converges_normally(tmp_path):
    root = _repo(tmp_path)
    spec = load_spec(_make(tmp_path, root=root))
    run = run_until_complete(spec, max_passes=3, fix_cmd=_fix_script(tmp_path, root=root),
                             patience=5,
                             write_allowlist=[str(root / f"{_modname(tmp_path)}.py"),
                                              str(root / "__pycache__")])
    assert run.complete and run.loop_reason == LoopReason.COMPLETE
    assert run.transcript[0].writeset_outside == []


# ── FIX-B regression: git-status alone is evadable ───────────────────────────
def test_a_fix_that_commits_its_own_writes_is_still_caught(tmp_path):
    # `git status` is clean after a commit, so a status-only audit would wave this through.
    root = _repo(tmp_path)
    spec = load_spec(_make(tmp_path, root=root))
    escape = root / "elsewhere" / "sneaky.txt"
    fix_cmd = (
        f"{sys.executable} -c \"import pathlib; p = pathlib.Path(r'{escape}'); "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('x')\" && "
        f"git -C {root} add -A && "
        f"git -C {root} -c user.name=t -c user.email=t@e commit -qm hidden"
    )
    run = run_until_complete(spec, max_passes=3, fix_cmd=fix_cmd, patience=5,
                             write_allowlist=[str(root / f"{_modname(tmp_path)}.py"),
                                              str(root / "__pycache__")])
    assert run.loop_reason == LoopReason.WRITESET_VIOLATION
    assert any("sneaky.txt" in p for p in run.transcript[-1].writeset_outside)


def test_a_gitignored_write_is_audited_too(tmp_path):
    # --ignored=matching: a fix cannot park a write in a gitignored path and audit clean.
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("secrets/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-qm", "ignore"], check=True)
    spec = load_spec(_make(tmp_path, root=root))
    hidden = root / "secrets" / "loot.txt"
    fix_cmd = (f"{sys.executable} -c \"import pathlib; p = pathlib.Path(r'{hidden}'); "
               "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('x')\"")
    run = run_until_complete(spec, max_passes=3, fix_cmd=fix_cmd, patience=5,
                             write_allowlist=[str(root / f"{_modname(tmp_path)}.py"),
                                              str(root / "__pycache__")])
    assert run.loop_reason == LoopReason.WRITESET_VIOLATION
    # git reports an ignored directory that matches an ignore pattern as the directory, so
    # the offender is `secrets/` rather than `secrets/loot.txt` — coarser, still outside.
    assert any(p.endswith("/secrets") for p in run.transcript[-1].writeset_outside)


def test_the_write_audit_fails_closed_when_it_cannot_run(tmp_path):
    # tmp_path is not a git work tree: the audit cannot see anything, so it must BLOCK.
    # An audit that cannot run and passes is worse than no audit — it reports safety.
    spec = load_spec(_make(tmp_path))
    run = run_until_complete(spec, max_passes=3, fix_cmd="true", patience=5,
                             write_allowlist=[str(tmp_path)])
    assert run.loop_reason == LoopReason.WRITESET_VIOLATION
    assert "not inside a git work tree" in run.transcript[-1].writeset_error


def test_no_write_allowlist_means_no_audit_and_no_change_in_behavior(tmp_path):
    # the audit is opt-in: without write_allowlist the loop behaves exactly as before,
    # even outside a git repo (where the audit could not have run anyway).
    run = run_until_complete(load_spec(_make(tmp_path)), max_passes=3,
                             fix_cmd=_fix_script(tmp_path), patience=5)
    assert run.complete and run.loop_reason == LoopReason.COMPLETE


def test_audit_writeset_reports_the_head_and_the_paths_it_checked(tmp_path):
    root = _repo(tmp_path)
    head = git_head(root)
    assert head and len(head) == 40
    (root / "seed.txt").write_text("edited\n", encoding="utf-8")
    inside = audit_writeset(root, [str(root / "seed.txt")], pre_head=head)
    assert inside.ok and not inside.outside and not inside.head_moved
    assert any(p.endswith("seed.txt") for p in inside.write_set)
    outside = audit_writeset(root, [str(root / "nothing")], pre_head=head)
    assert not outside.ok and outside.outside
    assert "wrote outside" in outside.summary()


def test_git_head_is_none_outside_a_repo(tmp_path):
    assert git_head(tmp_path) is None
