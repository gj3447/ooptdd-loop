# KG: OOPTDD_methodology_v1
"""Live watch harness — the fast inner loop (edit → save → verdict in seconds).

``ooptdd-loop watch <spec.yaml>`` runs the spec's target once, then keeps watching the
spec yaml, the target module source and every Longinus source with stdlib stat polling
(no file-watcher dependency — the jsonl backend's zero-infra philosophy applies to the
read path here too). Requirement verdicts re-render live, red→green, only on change.

Two cid regimes, mirroring ``runner.run_until_complete`` (see runner.py — re-producing
under a pinned cid double-counts ``op: "=="`` exact-count gates):

  * RUN mode (default) — a watched file changed ⇒ re-run the target under a **fresh cid**
    and re-judge. Nothing changed ⇒ idle. 고정 cid 재produce 는 절대 하지 않는다.
  * ATTACH mode (``--attach``) — never run the target; incrementally re-judge the events an
    EXTERNAL process ships under the pinned ``--cid``. Cross-process attach needs a
    queryable cross-process backend: jsonl (the store file doubles as the poll trigger) or
    openobserve. The memory backend is process-global, so attach only sees same-process
    ships (useful in tests); the otel backend is write-only and cannot be judged at all.

Judging is delegated wholesale to :func:`ooptdd_loop.runner.run_loop` (which is
``_produce_logs`` + ``evaluate_requirements``) — no verdict logic is duplicated here.
``kg_write``/``kg_store`` stay OFF: repeated judgment must not write the KG every tick.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

from .report import _check_miss, next_step_context, render
from .runner import RunResult, _new_cid, run_loop
from .domain.spec import Spec, load_spec

_UNSET = object()


def _stat_sig(path: str) -> tuple[int, int] | None:
    """(mtime_ns, size) of a path, or None when it does not exist / cannot be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def watched_paths(spec: Spec, spec_path: str) -> list[str]:
    """Everything whose change should re-trigger the loop: the spec file itself, the
    in_process target module source (best-effort ``mod.py`` / ``mod/__init__.py`` under
    root), the ontology file, and every Longinus source. Non-existent candidates are
    still watched — their creation is a change."""
    paths = [os.path.abspath(spec_path)]
    root = spec.target.root or "."

    def add(rel: str) -> None:
        p = os.path.abspath(os.path.join(root, rel))
        if p not in paths:
            paths.append(p)

    if spec.target.mode == "in_process" and spec.target.callable:
        mod = spec.target.callable.partition(":")[0]
        rel = mod.replace(".", os.sep)
        add(rel + ".py")
        add(os.path.join(rel, "__init__.py"))
    if spec.target.ontology:
        add(spec.target.ontology)
    for req in spec.requirements:
        if req.longinus and req.longinus.source:
            add(req.longinus.source)
    return paths


def _store_path(spec: Spec) -> str | None:
    """The watchable store file, if the backend has one (jsonl only). Its mtime/size is
    the cheapest 'new events arrived' signal for cross-process attach."""
    if spec.target.backend != "jsonl":
        return None
    opts = spec.target.backend_options
    p = opts.get("path") or os.getenv(opts.get("path_env", "OOPTDD_JSONL_PATH"), "")
    return os.path.abspath(p) if p else None


@dataclass
class WatchTick:
    """One evaluated poll. ``tick`` is the poll counter (idle polls return None and leave
    gaps). ``trigger``: initial | file_change | events | poll. ``changed`` compares the
    requirement-state snapshot (not the cid) against the previous evaluation."""

    tick: int
    trigger: str
    changed: bool
    changed_files: list[str] = field(default_factory=list)
    run: RunResult | None = None
    error: str | None = None
    ts: float = 0.0


def run_payload(run: RunResult) -> dict:
    """Machine-readable requirement rows — shared by ``watch --json`` lines and the
    ``watch_tick`` MCP tool (one line/one call ≙ one verdict, agent-parseable)."""
    return {
        "cid": run.cid,
        "backend": run.backend,
        "complete": run.complete,
        "done": run.n_done,
        "total": len(run.results),
        "methodology_ok": run.methodology_ok,
        "requirements": [
            {
                "id": r.id,
                "gate_ok": r.gate_ok,
                "reachable": r.reachable,
                "bound": r.bound,
                "done": r.done,
                # precise miss reasons (undelivered events, count mismatches, order gaps)
                "miss": [_check_miss(c) for c in r.checks if not c.get("passed")],
            }
            for r in run.results
        ],
    }


def tick_payload(t: WatchTick) -> dict:
    if t.error is not None:
        return {"type": "watch_error", "tick": t.tick, "trigger": t.trigger,
                "changed_files": t.changed_files, "error": t.error, "ts": t.ts}
    return {"type": "watch_tick", "tick": t.tick, "trigger": t.trigger,
            "changed": t.changed, "changed_files": t.changed_files,
            **run_payload(t.run), "ts": t.ts}


def _watch_state(run: RunResult):
    """Change-detection snapshot: per-requirement verdict PLUS the failed-check texts, so
    a rising got-count under the same RED verdict still counts as visible progress.
    The cid is deliberately excluded — a re-run with an identical verdict is 'no change'."""
    return (
        tuple(
            (r.id, r.gate_ok, r.reachable, r.bound, r.done,
             tuple(_check_miss(c) for c in r.checks if not c.get("passed")))
            for r in run.results
        ),
        run.methodology_ok,
    )


class Watcher:
    """Poll-driven live harness. Call :meth:`tick` yourself (tests/embedding) or drive it
    with :meth:`loop`. Evaluation errors (mid-edit syntax errors, broken imports) become
    error ticks — transient, distinct from a RED verdict — and the watcher waits for the
    next file change instead of hammering a broken target."""

    def __init__(self, spec_path: str, *, cid: str | None = None, attach: bool = False):
        self.spec_path = os.path.abspath(spec_path)
        self.attach = attach
        self.spec = load_spec(self.spec_path)
        if attach:
            self.cid = cid or os.getenv("OOPTDD_CID")
            if not self.cid:
                raise ValueError(
                    "--attach needs --cid (or $OOPTDD_CID): the correlation id the "
                    "external process ships its events under")
        else:
            self.cid = cid or _new_cid("watch")
        self._tick_no = 0
        self._sigs: dict[str, tuple[int, int] | None] = {}
        self._store_sig = _UNSET
        self._prev_state = None
        self._last_error: str | None = None
        self.last_run: RunResult | None = None

    # ── polling ──────────────────────────────────────────────────────────────
    def _scan_sources(self) -> list[str]:
        """Changed watched paths since the previous scan. The first scan is the baseline
        (returns []); a path first seen on a later scan (e.g. added by a spec reload) is
        baselined silently — the spec change itself already triggered."""
        changed = []
        for p in watched_paths(self.spec, self.spec_path):
            sig = _stat_sig(p)
            if p in self._sigs and self._sigs[p] != sig:
                changed.append(p)
            self._sigs[p] = sig
        return changed

    def _scan_store(self) -> bool | None:
        """True/False = the watchable store file changed / didn't; None = no watchable
        store (memory, openobserve, …) so arrival can only be seen by re-querying."""
        sp = _store_path(self.spec)
        if sp is None:
            self._store_sig = _UNSET
            return None
        prev, sig = self._store_sig, _stat_sig(sp)
        self._store_sig = sig
        return False if prev is _UNSET else prev != sig

    # ── one poll ─────────────────────────────────────────────────────────────
    def tick(self) -> WatchTick | None:
        """One poll: detect changes, decide the trigger, re-run/re-judge, snapshot-diff.
        Returns None when idle (nothing changed and nothing to re-query)."""
        self._tick_no += 1
        now = time.time()
        first = self._tick_no == 1
        changed_files = self._scan_sources()
        store_changed = self._scan_store()

        if first:
            trigger = "initial"
        elif changed_files:
            trigger = "file_change"
        elif store_changed:
            trigger = "events"
        elif self.attach and store_changed is None:
            trigger = "poll"      # no store file to watch — arrival is only visible by re-query
        else:
            return None

        if not first and self.spec_path in changed_files:
            try:  # mid-edit yaml must not kill the loop; keep judging with the old spec
                self.spec = load_spec(self.spec_path)
            except Exception as e:  # noqa: BLE001
                return self._error_tick(trigger, changed_files, f"spec reload failed: {e}", now)

        produce = (not self.attach) and (first or bool(changed_files))
        if produce and not first:
            # fresh cid per re-run — re-producing a pinned cid double-counts `op: "=="` gates
            self.cid = _new_cid("watch")
        try:
            run = run_loop(self.spec, cid=self.cid, produce=produce)
        except Exception as e:  # noqa: BLE001 — mid-edit import/run crash = transient, not RED
            return self._error_tick(trigger, changed_files, f"{type(e).__name__}: {e}", now)
        if produce:
            self._scan_store()  # absorb our own store write so it doesn't retrigger next poll

        state = _watch_state(run)
        changed = state != self._prev_state
        self._prev_state = state
        self._last_error = None
        self.last_run = run
        return WatchTick(tick=self._tick_no, trigger=trigger, changed=changed,
                         changed_files=changed_files, run=run, ts=now)

    def _error_tick(self, trigger: str, changed_files: list[str], err: str,
                    ts: float) -> WatchTick:
        changed = err != self._last_error
        self._last_error = err
        return WatchTick(tick=self._tick_no, trigger=trigger, changed=changed,
                         changed_files=changed_files, error=err, ts=ts)

    # ── the loop ─────────────────────────────────────────────────────────────
    def loop(self, *, interval: float = 0.5, until_complete: bool = False,
             timeout: float | None = None, max_ticks: int = 0, on_tick=None) -> int:
        """Drive :meth:`tick` until COMPLETE (``until_complete`` ⇒ exit 0), the tick/time
        budget is spent (exit by the last verdict), or forever (the default watch)."""
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            t = self.tick()
            if t is not None and on_tick is not None:
                on_tick(t)
            if until_complete and t is not None and t.run is not None and t.run.complete:
                return 0
            if max_ticks and self._tick_no >= max_ticks:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if interval:
                time.sleep(interval)
        return 0 if (self.last_run is not None and self.last_run.complete) else 1


# ── CLI adapter ──────────────────────────────────────────────────────────────
def _emit(t: WatchTick, *, json_mode: bool) -> None:
    """Redraw only when something is worth saying: any state change, and evaluated
    triggers (file_change/events) even without one — silent on unchanged idle polls."""
    if t.error is not None:
        if t.changed or t.trigger in ("initial", "file_change"):
            if json_mode:
                print(json.dumps(tick_payload(t), ensure_ascii=False), flush=True)
            else:
                print(f"[watch] tick={t.tick} {t.trigger} RUN ERROR (transient — "
                      f"fix & save to retry): {t.error}", file=sys.stderr, flush=True)
        return
    if json_mode:
        if t.changed or t.trigger in ("initial", "file_change", "events"):
            print(json.dumps(tick_payload(t), ensure_ascii=False), flush=True)
        return
    header = f"[watch] tick={t.tick} trigger={t.trigger}"
    if t.changed_files:
        header += " changed=" + ",".join(os.path.basename(p) for p in t.changed_files)
    if t.changed or t.trigger == "initial":
        print(header, flush=True)
        print(render(t.run) + "\n", flush=True)
    elif t.trigger in ("file_change", "events"):
        print(f"{header} — no state change "
              f"({t.run.n_done}/{len(t.run.results)} DONE)", flush=True)


def watch_command(args) -> int:
    """`ooptdd-loop watch` entry — argparse Namespace in, exit code out."""
    try:
        watcher = Watcher(args.spec, cid=args.cid, attach=args.attach)
    except ValueError as e:
        print(f"watch: {e}", file=sys.stderr)
        return 2
    try:
        rc = watcher.loop(
            interval=args.interval,
            until_complete=args.until_complete,
            timeout=args.timeout,
            max_ticks=args.max_ticks,
            on_tick=lambda t: _emit(t, json_mode=args.json),
        )
    except KeyboardInterrupt:
        rc = 0 if (watcher.last_run is not None and watcher.last_run.complete) else 1
    if rc != 0 and not args.json and watcher.last_run is not None:
        ctx = next_step_context(watcher.last_run)
        if ctx:
            print("\n" + ctx, file=sys.stderr)
    return rc
