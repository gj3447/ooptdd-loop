"""watch — the live inner loop: red→green as events arrive, re-run on file change.

Follows the test_fixpoint_loop pattern: tmp_path target module + spec yaml, a unique
module name per test (import-cache isolation), memory_reset around every test. The
watcher is driven tick-by-tick (no sleeps) except for the CLI smoke tests, which use
--interval 0 with a tick/budget bound.
"""
from __future__ import annotations

import json
import os

import pytest

from ooptdd.backends import get_backend, memory_reset
from ooptdd_loop import tools
from ooptdd_loop.cli import main
from ooptdd_loop.watch import Watcher, tick_payload, watched_paths

GREEN_BODY = 'def run(backend, cid):\n    backend.ship([{"cid": cid, "event": "ping"}])\n'
RED_BODY = "def run(backend, cid):\n    pass\n"
BROKEN_BODY = "def run(backend, cid:\n"          # mid-edit syntax error


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


def _make(tmp_path, *, emits: bool, **kw) -> str:
    mod = _modname(tmp_path)
    (tmp_path / f"{mod}.py").write_text(GREEN_BODY if emits else RED_BODY, encoding="utf-8")
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
