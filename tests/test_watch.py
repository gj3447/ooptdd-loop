"""watch — the live inner loop: red→green as events arrive, re-run on file change.

Follows the test_fixpoint_loop pattern: tmp_path target module + spec yaml, a unique
module name per test (import-cache isolation), memory_reset around every test. The
watcher is driven tick-by-tick (no sleeps) except for the CLI smoke tests, which use
--interval 0 with a tick/budget bound. Both RUN-mode backends are constrained: memory
(no store file → poll re-query) and jsonl (store file = trigger + self-write absorption,
including on the produce-crash path — the forgery regression)."""
from __future__ import annotations

import json
import os
import time

import pytest

from ooptdd.backends import get_backend, memory_reset
from ooptdd_loop import tools
from ooptdd_loop.cli import main
from ooptdd_loop.watch import Watcher, tick_payload, watched_paths

GREEN_BODY = 'def run(backend, cid):\n    backend.ship([{"cid": cid, "event": "ping"}])\n'
RED_BODY = "def run(backend, cid):\n    pass\n"
BROKEN_BODY = "def run(backend, cid:\n"          # mid-edit syntax error
# ships real evidence, THEN dies — partial evidence of a crashed run must never judge green
CRASH_AFTER_SHIP_BODY = ('def run(backend, cid):\n'
                         '    backend.ship([{"cid": cid, "event": "ping"}])\n'
                         '    raise RuntimeError("crashed AFTER shipping")\n')


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _modname(tmp_path) -> str:
    return "wsvc_" + os.path.basename(str(tmp_path)).replace("-", "_")


def _spec_yaml(tmp_path, mod: str, *, backend: str = "memory", store: str | None = None,
               count: int = 1) -> str:
    opts = f"\n  backend_options: {{path: {store}}}" if store else ""
    return f"""
target:
  mode: in_process
  callable: {mod}:run
  backend: {backend}{opts}
  root: {tmp_path}
requirements:
  - id: REQ-PING
    description: a ping is emitted
    gate: [{{event: ping, op: "==", count: {count}}}]
    longinus: {{kg_anchor: 'ref:ping', source: {mod}.py, symbol: run, must_emit: ping}}
"""


def _make(tmp_path, *, emits: bool = True, body: str | None = None, **kw) -> str:
    mod = _modname(tmp_path)
    body = body if body is not None else (GREEN_BODY if emits else RED_BODY)
    (tmp_path / f"{mod}.py").write_text(body, encoding="utf-8")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(_spec_yaml(tmp_path, mod, **kw), encoding="utf-8")
    return str(spec_path)


# ── run mode: initial verdict, idle, file-change re-run ─────────────────────────
def test_run_mode_green_first_tick_then_idle(tmp_path):
    w = Watcher(_make(tmp_path, emits=True))
    t = w.tick()
    assert t is not None and t.trigger == "initial" and t.changed
    assert t.run.complete and t.error is None
    assert w.tick() is None                     # nothing changed → idle, no re-run


def test_file_change_triggers_rerun_with_fresh_cid(tmp_path):
    spec = _make(tmp_path, emits=False)
    w = Watcher(spec)
    t1 = w.tick()
    assert not t1.run.complete and t1.run.results[0].gate_ok is False
    (tmp_path / f"{_modname(tmp_path)}.py").write_text(GREEN_BODY, encoding="utf-8")
    t2 = w.tick()
    assert t2.trigger == "file_change" and t2.changed
    assert t2.run.complete
    assert t2.run.cid != t1.run.cid             # fresh cid — no ==-gate double count


def test_spec_edit_reloads_and_rejudges(tmp_path):
    # gate wants 2 pings, the target ships 1 → RED; editing the SPEC to count 1 → GREEN
    spec = _make(tmp_path, emits=True, count=2)
    w = Watcher(spec)
    assert not w.tick().run.complete
    (tmp_path / "spec.yaml").write_text(
        _spec_yaml(tmp_path, _modname(tmp_path), count=1), encoding="utf-8")
    t = w.tick()
    assert t.trigger == "file_change" and t.run.complete


def test_broken_target_is_transient_error_not_red(tmp_path):
    spec = _make(tmp_path, emits=True)
    mod_py = tmp_path / f"{_modname(tmp_path)}.py"
    mod_py.write_text(BROKEN_BODY, encoding="utf-8")
    w = Watcher(spec)
    t1 = w.tick()
    assert t1.error is not None and t1.run is None and "SyntaxError" in t1.error
    assert w.tick() is None                     # broken: wait for the next save, don't hammer
    mod_py.write_text(GREEN_BODY, encoding="utf-8")
    t2 = w.tick()
    assert t2.error is None and t2.run.complete


# ── run mode + jsonl: self-write absorption, external events, the forgery path ──
def test_run_jsonl_self_write_absorbed_then_external_events_rejudge(tmp_path):
    store = tmp_path / "events.jsonl"
    w = Watcher(_make(tmp_path, emits=True, backend="jsonl", store=str(store)))
    t1 = w.tick()
    assert t1.trigger == "initial" and t1.run.complete
    assert w.tick() is None                     # own store write absorbed — no retrigger
    # an EXTERNAL write under the same cid re-judges honestly: == 1 gate sees got 2 → RED
    get_backend("jsonl", path=str(store)).ship([{"cid": w.cid, "event": "ping"}])
    t3 = w.tick()
    assert t3.trigger == "events" and not t3.run.complete
    assert any("got 2" in m for m in tick_payload(t3)["requirements"][0]["miss"])


def test_run_jsonl_crash_after_ship_never_turns_complete(tmp_path):
    """The forgery regression: a target that ships evidence and THEN crashes must never
    be judged COMPLETE — its partial store write is absorbed on the error path and its
    cid retired, so neither the next tick nor any later `events` trigger can re-judge
    the crashed run green (`ooptdd-loop run` on the same spec propagates the crash)."""
    store = tmp_path / "events.jsonl"
    spec = _make(tmp_path, body=CRASH_AFTER_SHIP_BODY, backend="jsonl", store=str(store))
    w = Watcher(spec)
    t1 = w.tick()
    assert t1.error is not None and "RuntimeError" in t1.error and t1.run is None
    assert w.tick() is None                     # partial self-write absorbed → idle, not events
    # even a genuine external store write must not resurrect the crashed cid
    get_backend("jsonl", path=str(store)).ship([{"cid": "someone-else", "event": "ping"}])
    t3 = w.tick()
    assert t3 is not None and t3.trigger == "events" and not t3.run.complete
    assert w.loop(interval=0, until_complete=True, max_ticks=6) == 1


def test_run_crash_after_ship_waits_for_save_then_fresh_cid_wins(tmp_path):
    # memory backend: after a produce crash the watcher neither polls a verdict out of the
    # crashed run nor reuses its cid — the next SAVE re-runs under a fresh cid, so the
    # crashed run's stray ping cannot double-count the == 1 gate.
    spec = _make(tmp_path, body=CRASH_AFTER_SHIP_BODY)
    w = Watcher(spec)
    assert w.tick().error is not None
    assert w.tick() is None                     # error state: wait for the next save
    (tmp_path / f"{_modname(tmp_path)}.py").write_text(GREEN_BODY, encoding="utf-8")
    t3 = w.tick()
    assert t3.trigger == "file_change" and t3.error is None and t3.run.complete


# ── run mode + storeless backends: late evidence re-judged by poll ───────────────
def test_run_mode_polls_late_events_on_storeless_backend(tmp_path):
    # gate wants exactly 2 pings, the target ships 1 → RED. The second ping arrives
    # LATE (async ingest / another thread). memory has no store file to watch, so the
    # poll re-query — not a file change — must flip the verdict green.
    w = Watcher(_make(tmp_path, emits=True, count=2))
    t1 = w.tick()
    assert not t1.run.complete
    t_poll = w.tick()
    assert t_poll is not None and t_poll.trigger == "poll" and not t_poll.changed
    get_backend("memory").ship([{"cid": w.cid, "event": "ping"}])
    t2 = w.tick()
    assert t2.trigger == "poll" and t2.changed and t2.run.complete
    assert w.tick() is None                     # complete → idle, the verdict is not churned


# ── attach mode: incremental re-judgment of a pinned cid ────────────────────────
def test_attach_rejudges_as_events_arrive(tmp_path):
    spec = _make(tmp_path, emits=True)          # source holds the emitter; we never run it
    w = Watcher(spec, cid="attach-1", attach=True)
    t1 = w.tick()
    assert t1.trigger == "initial" and not t1.run.complete
    assert t1.run.cid == "attach-1"
    t_idle = w.tick()                           # memory: no store file → re-query every poll
    assert t_idle is not None and t_idle.trigger == "poll" and not t_idle.changed
    get_backend("memory").ship([{"cid": "attach-1", "event": "ping"}])
    t2 = w.tick()
    assert t2.changed and t2.run.complete       # red→green with no target run


def test_attach_requires_cid(tmp_path, monkeypatch):
    monkeypatch.delenv("OOPTDD_CID", raising=False)
    with pytest.raises(ValueError):
        Watcher(_make(tmp_path, emits=True), attach=True)


def test_attach_jsonl_store_file_is_the_trigger(tmp_path):
    store = tmp_path / "events.jsonl"
    spec = _make(tmp_path, emits=True, backend="jsonl", store=str(store))
    w = Watcher(spec, cid="xproc-1", attach=True)
    assert not w.tick().run.complete            # initial: store file absent → absent (⊥)
    assert w.tick() is None                     # store unchanged → idle (no full reread)
    # the "external process": ship through the store file, as another process would
    get_backend("jsonl", path=str(store)).ship([{"cid": "xproc-1", "event": "ping"}])
    t = w.tick()
    assert t.trigger == "events" and t.changed and t.run.complete


def test_attach_event_arriving_with_broken_spec_is_not_lost(tmp_path):
    """Lost-wakeup regression: an external event and a broken spec save land in the SAME
    poll. The reload error must not consume the event trigger — the next tick re-detects
    the store change and judges it with the OLD spec (as the code comment promises)."""
    store = tmp_path / "events.jsonl"
    spec = _make(tmp_path, emits=True, backend="jsonl", store=str(store))
    good_yaml = (tmp_path / "spec.yaml").read_text(encoding="utf-8")
    w = Watcher(spec, cid="xproc-2", attach=True)
    assert not w.tick().run.complete
    get_backend("jsonl", path=str(store)).ship([{"cid": "xproc-2", "event": "ping"}])
    (tmp_path / "spec.yaml").write_text("target: [broken", encoding="utf-8")
    t2 = w.tick()
    assert t2.error is not None and "spec reload failed" in t2.error
    t3 = w.tick()                               # the shipped event re-triggers — old spec judges
    assert t3 is not None and t3.trigger == "events" and t3.run.complete
    (tmp_path / "spec.yaml").write_text(good_yaml, encoding="utf-8")
    t4 = w.tick()                               # the fixed yaml still re-triggers a reload
    assert t4 is not None and t4.error is None


def test_run_module_edit_with_broken_spec_same_poll_is_not_lost(tmp_path):
    # same lost-wakeup class, run mode: a module edit co-arriving with a broken spec save
    # must re-trigger the re-run on the NEXT tick instead of being consumed by the error.
    spec = _make(tmp_path, emits=False)
    w = Watcher(spec)
    assert not w.tick().run.complete
    (tmp_path / f"{_modname(tmp_path)}.py").write_text(GREEN_BODY, encoding="utf-8")
    (tmp_path / "spec.yaml").write_text("target: [broken", encoding="utf-8")
    t2 = w.tick()
    assert t2.error is not None and "spec reload failed" in t2.error
    t3 = w.tick()
    assert t3 is not None and t3.trigger == "file_change" and t3.run.complete


# ── helper modules: watched and re-imported fresh ────────────────────────────────
def test_helper_module_edit_retriggers_and_runs_fresh_code(tmp_path):
    # the SUT = entry module + helper module; the gate's emitter lives in the HELPER.
    # Editing the helper must (a) re-trigger the loop and (b) actually run the edited
    # code — not the import-cached stale version (runner only evicts the entry module).
    base = _modname(tmp_path)
    entry, helper = f"{base}_entry", f"{base}_help"
    (tmp_path / f"{helper}.py").write_text("def emit(backend, cid):\n    pass\n",
                                           encoding="utf-8")
    (tmp_path / f"{entry}.py").write_text(
        f"import {helper}\ndef run(backend, cid):\n    {helper}.emit(backend, cid)\n",
        encoding="utf-8")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(f"""
target:
  mode: in_process
  callable: {entry}:run
  backend: memory
  root: {tmp_path}
requirements:
  - id: REQ-PING
    description: the helper ships a ping
    gate: [{{event: ping, op: "==", count: 1}}]
    longinus: {{kg_anchor: 'ref:ping', source: {helper}.py, symbol: emit, must_emit: ping}}
""", encoding="utf-8")
    w = Watcher(str(spec_path))
    assert not w.tick().run.complete            # helper doesn't emit yet → RED
    w.tick()                                    # baselines the discovered helper path
    (tmp_path / f"{helper}.py").write_text(GREEN_BODY.replace("def run", "def emit"),
                                           encoding="utf-8")
    t = w.tick()
    assert t.trigger == "file_change"
    assert str(tmp_path / f"{helper}.py") in t.changed_files
    assert t.run.complete                       # fresh helper code ran, not the cached stub


# ── long sessions: a GREEN verdict must not expire out of the lookback window ────
def test_long_session_green_does_not_expire(tmp_path, monkeypatch):
    spec = _make(tmp_path, emits=True)
    w = Watcher(spec, cid="long-1", attach=True)
    get_backend("memory").ship([{"cid": "long-1", "event": "ping"}])
    assert w.tick().run.complete
    real_now = time.time()                      # 2h later — far past the 1h default lookback
    monkeypatch.setattr(time, "time", lambda: real_now + 7200)
    t = w.tick()
    assert t is not None and t.run.complete and not t.changed


# ── loop budget / exit codes ─────────────────────────────────────────────────────
def test_loop_until_complete_exits_zero(tmp_path):
    w = Watcher(_make(tmp_path, emits=True))
    assert w.loop(interval=0, until_complete=True, max_ticks=3) == 0


def test_loop_budget_spent_while_red_exits_one(tmp_path):
    w = Watcher(_make(tmp_path, emits=False))
    assert w.loop(interval=0, until_complete=True, max_ticks=2) == 1


def test_loop_timeout_exits_one_when_incomplete(tmp_path):
    w = Watcher(_make(tmp_path, emits=False))
    assert w.loop(interval=0, until_complete=True, timeout=0) == 1


# ── --json line schema ───────────────────────────────────────────────────────────
def test_tick_payload_schema_red_includes_miss(tmp_path):
    w = Watcher(_make(tmp_path, emits=False))
    p = tick_payload(w.tick())
    assert p["type"] == "watch_tick" and p["trigger"] == "initial"
    assert p["complete"] is False and p["done"] == 0 and p["total"] == 1
    assert {"tick", "changed", "changed_files", "cid", "backend",
            "methodology_ok", "requirements", "ts"} <= set(p)
    req = p["requirements"][0]
    assert {"id", "gate_ok", "reachable", "bound", "done", "miss"} <= set(req)
    assert req["miss"] and "ping" in req["miss"][0]   # the undelivered event, by name
    assert json.loads(json.dumps(p)) == p             # fully JSON-serializable


def test_watched_paths_cover_spec_target_and_longinus(tmp_path):
    spec = _make(tmp_path, emits=True)
    mod = _modname(tmp_path)
    paths = watched_paths(Watcher(spec).spec, spec)
    assert str(tmp_path / "spec.yaml") in paths
    assert str(tmp_path / f"{mod}.py") in paths       # target module == longinus source here


# ── CLI smoke (test_cli_harness pattern) ────────────────────────────────────────
def test_cli_watch_until_complete_green(tmp_path, capsys):
    spec = _make(tmp_path, emits=True)
    rc = main(["watch", spec, "--json", "--until-complete", "--max-ticks", "3",
               "--interval", "0"])
    assert rc == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert lines and lines[0]["type"] == "watch_tick" and lines[-1]["complete"] is True


def test_cli_watch_red_budget_exits_one_with_context(tmp_path, capsys):
    spec = _make(tmp_path, emits=False)
    rc = main(["watch", spec, "--max-ticks", "1", "--interval", "0"])
    assert rc == 1
    out = capsys.readouterr()
    assert "REQ-PING" in out.out and "gate=RED" in out.out
    assert "NEXT STEP" in out.err                     # agent hand-off on incomplete exit


def test_cli_watch_attach_without_cid_is_usage_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OOPTDD_CID", raising=False)
    rc = main(["watch", _make(tmp_path, emits=True), "--attach", "--max-ticks", "1",
               "--interval", "0"])
    assert rc == 2
    assert "--cid" in capsys.readouterr().err


# ── MCP one-shot tool ────────────────────────────────────────────────────────────
def test_watch_tick_tool_incremental_judgment(tmp_path):
    spec = _make(tmp_path, emits=True)
    out1 = tools.call("watch_tick", spec=spec, cid="wt-1")
    assert out1["complete"] is False and out1["next_step"]
    get_backend("memory").ship([{"cid": "wt-1", "event": "ping"}])
    out2 = tools.call("watch_tick", spec=spec, cid="wt-1")
    assert out2["complete"] is True and out2["next_step"] == ""


def test_watch_tick_tool_can_produce(tmp_path):
    spec = _make(tmp_path, emits=True)
    out = tools.call("watch_tick", spec=spec, cid="wt-2", produce=True)
    assert out["complete"] is True
    assert out["requirements"][0]["miss"] == []


def test_watch_tick_tool_refuses_reproducing_a_pinned_cid(tmp_path):
    # watch_tick's cid is caller-pinned; produce=true on a cid that already has events
    # would double-count `op: "=="` gates (green→false RED here; a crashed partial run
    # →false GREEN elsewhere). The tool must refuse, and produce=false must still judge.
    spec = _make(tmp_path, emits=True)
    assert tools.call("watch_tick", spec=spec, cid="wt-3", produce=True)["complete"] is True
    with pytest.raises(ValueError, match="already shipped"):
        tools.call("watch_tick", spec=spec, cid="wt-3", produce=True)
    out = tools.call("watch_tick", spec=spec, cid="wt-3")     # re-judge stays green
    assert out["complete"] is True


def test_run_and_watch_tick_share_one_payload_schema(tmp_path):
    # one canonical payload (report.run_payload): an agent hopping between the `run`
    # tool, the `watch_tick` tool and `watch --json` rows must see identical schemas.
    spec = _make(tmp_path, emits=True)
    run_out = tools.call("run", spec=spec)
    wt_out = tools.call("watch_tick", spec=spec, cid=run_out["cid"])
    watch_row = tick_payload(Watcher(spec).tick())
    assert set(run_out) == set(wt_out)
    assert set(run_out) <= set(watch_row) | {"next_step"}
    for payload in (run_out, wt_out, watch_row):
        assert {"methodology_checks", "requirements"} <= set(payload)
        assert {"id", "gate_ok", "reachable", "bound", "done", "miss"} == set(
            payload["requirements"][0])
