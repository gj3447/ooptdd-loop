# KG: OOPTDD_methodology_v1
"""Live watch harness — the fast inner loop (edit → save → verdict in seconds).

``ooptdd-loop watch <spec.yaml>`` runs the spec's target once, then keeps watching the
spec yaml, the target module source and every Longinus source with stdlib stat polling
(no file-watcher dependency — the jsonl backend's zero-infra philosophy applies to the
read path here too). Requirement verdicts re-render live, red→green, only on change.

Two cid regimes, mirroring ``runner.run_until_complete`` (see runner.py — re-producing
under a pinned cid double-counts ``op: "=="`` exact-count gates):

  * RUN mode (default) — a watched file changed ⇒ re-run the target under a **fresh cid**
    and re-judge. While the verdict is RED and the backend has no watchable store file
    (memory/openobserve/…), each poll RE-QUERIES the current cid (``produce=False``) so
    late-arriving evidence — async ingest, a late same-process ship — still flips
    RED→GREEN; a COMPLETE verdict goes idle. 고정 cid 재produce 는 절대 하지 않는다. A
    produce that CRASHES retires its cid: partial events shipped before the crash must
    never be re-judged into a COMPLETE (that would be the loop-forgery class bde2876
    closed, reopened through watch).
  * ATTACH mode (``--attach``) — never run the target; incrementally re-judge the events an
    EXTERNAL process ships under the pinned ``--cid``. Cross-process attach needs a
    queryable cross-process backend: jsonl (the store file doubles as the poll trigger) or
    openobserve. The memory backend is process-global, so attach only sees same-process
    ships (useful in tests); the otel backend is write-only and cannot be judged at all.

Judging is delegated wholesale to :func:`ooptdd_loop.runner.run_loop` (which is
``_produce_logs`` + ``evaluate_requirements``) — no verdict logic is duplicated here.
The backend handed to it pins the gate query window's START at watcher start
(:class:`_SessionWindowBackend`): watch is the first surface that outlives the backends'
default 1h lookback, and a GREEN verdict must not expire just because the session got old.
``kg_write``/``kg_store`` stay OFF: repeated judgment must not write the KG every tick.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

from ooptdd.backends import get_backend

from .report import _check_miss, next_step_context, render, run_payload
from .runner import RunResult, _new_cid, run_loop
from .domain.spec import Spec, load_spec

__all__ = ["Watcher", "WatchTick", "run_payload", "tick_payload", "watch_command",
           "watched_paths"]

_UNSET = object()


def _stat_sig(path: str) -> tuple[int, int] | None:
    """(mtime_ns, size) of a path, or None when it does not exist / cannot be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def watched_paths(spec: Spec, spec_path: str) -> list[str]:
    """Everything statically knowable whose change should re-trigger the loop: the spec
    file itself, the in_process target module source (``mod.py`` / ``mod/__init__.py``
    under root), the ontology file, and every Longinus source. Non-existent candidates
    are still watched — their creation is a change. Helper modules the target imports
    are dynamic (only a run reveals them): the Watcher discovers those from
    ``sys.modules`` after each produce (:func:`_modules_under_root`) and watches them
    too."""
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


# dirs under root that are never the SUT's own source (vendored/derived code), and the
# harness's own packages — evicting THOSE from sys.modules would leave the running
# watcher holding stale duplicate module objects.
_VENDOR_DIRS = {"site-packages", "dist-packages", "__pycache__", ".venv", "venv",
                "node_modules", ".git"}
_PROTECTED_PKGS = {"ooptdd", "ooptdd_loop", "__main__"}


def _modules_under_root(root: str) -> dict[str, str]:
    """Loaded modules whose source file lives under ``root``: module name → abspath.
    This is the SUT's real import closure — the entry module plus every helper it pulled
    in — which only a run can reveal. Used twice: to WATCH helper files (an edit must
    re-trigger) and to EVICT them before a re-run (runner pops only the entry module, so
    an edited helper would otherwise run as stale cached code)."""
    rootp = os.path.abspath(root) + os.sep
    found: dict[str, str] = {}
    for name, mod in list(sys.modules.items()):
        if mod is None or name.partition(".")[0] in _PROTECTED_PKGS:
            continue
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        f = os.path.abspath(f)
        if not f.startswith(rootp) or _VENDOR_DIRS.intersection(
                f[len(rootp):].split(os.sep)):
            continue
        found[name] = f
    return found


class _SessionWindowBackend:
    """Backend proxy for long-lived watch sessions: ``default_lookback_s`` grows with the
    session's age, pinning the gate query window's START at (session start − base
    lookback) instead of letting it slide with wall-clock (engine._query_events queries
    [now − lookback, now + future_buffer]). Without this, an ``op: "=="`` gate that went
    GREEN flips to RED once its events age out of the sliding window (~1h for
    memory/jsonl) — a verdict must not expire merely because the watcher kept running.
    Everything else (ship/query/caps/…) proxies to the real backend untouched."""

    def __init__(self, inner, session_t0: float):
        self._inner = inner
        self._session_t0 = session_t0

    @property
    def default_lookback_s(self):
        return self._inner.default_lookback_s + max(0.0, time.time() - self._session_t0)

    def __getattr__(self, name):
        return getattr(self._inner, name)


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


def tick_payload(t: WatchTick) -> dict:
    # requirement rows come from report.run_payload — the ONE canonical payload shape,
    # shared with the `run`/`watch_tick` MCP tools (no hand-rolled schema drift here).
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
        self._session_t0 = time.time()   # pins the gate query window's start (see proxy)
        self._sigs: dict[str, tuple[int, int] | None] = {}
        self._store_sig = _UNSET
        self._helper_paths: list[str] = []   # under-root modules the last run imported
        self._prev_state = None
        self._last_error: str | None = None
        self.last_run: RunResult | None = None

    # ── polling ──────────────────────────────────────────────────────────────
    def _scan_sources(self) -> list[str]:
        """Changed watched paths since the previous scan. The first scan is the baseline
        (returns []); a path first seen on a later scan (e.g. added by a spec reload) is
        baselined silently — the spec change itself already triggered."""
        changed = []
        for p in dict.fromkeys(watched_paths(self.spec, self.spec_path)
                               + self._helper_paths):
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

    def _should_poll(self) -> bool:
        """Whether a no-store-file tick still re-queries (trigger ``poll``). Attach mode
        always does — the pinned cid is fed by an external process, so arrival is only
        ever visible by re-query. Run mode polls exactly while the last verdict is RED
        and the target isn't broken: late evidence (async ingest on openobserve, a late
        same-process ship on memory) must still flip RED→GREEN, but a COMPLETE verdict
        is not churned, and an error state waits for the next save (don't judge a
        crashed run's cid into a verdict)."""
        if self.attach:
            return True
        return (self._last_error is None
                and self.last_run is not None and not self.last_run.complete)

    def _backend(self):
        """The spec's backend (rebuilt per tick — a spec reload may repoint it), wrapped
        so the gate query window stays pinned to the session start (see
        :class:`_SessionWindowBackend`)."""
        inner = get_backend(self.spec.target.backend, **self.spec.target.backend_options)
        return _SessionWindowBackend(inner, self._session_t0)

    def _evict_sut_modules(self) -> None:
        """Fresh re-import for the whole under-root module set before a produce, not just
        the entry module (runner pops only that): an edited HELPER module must not run
        as stale cached code."""
        if self.spec.target.mode != "in_process":
            return
        for name in _modules_under_root(self.spec.target.root or "."):
            sys.modules.pop(name, None)

    # ── one poll ─────────────────────────────────────────────────────────────
    def tick(self) -> WatchTick | None:
        """One poll: detect changes, decide the trigger, re-run/re-judge, snapshot-diff.
        Returns None when idle (nothing changed and nothing to re-query)."""
        self._tick_no += 1
        now = time.time()
        first = self._tick_no == 1
        prev_sigs = dict(self._sigs)         # for un-consuming triggers on a broken spec
        prev_store_sig = self._store_sig
        changed_files = self._scan_sources()
        store_changed = self._scan_store()

        if first:
            trigger = "initial"
        elif changed_files:
            trigger = "file_change"
        elif store_changed:
            trigger = "events"
        elif store_changed is None and self._should_poll():
            trigger = "poll"      # no store file to watch — arrival is only visible by re-query
        else:
            return None

        if not first and self.spec_path in changed_files:
            try:  # mid-edit yaml must not kill the loop; keep judging with the old spec
                self.spec = load_spec(self.spec_path)
            except Exception as e:  # noqa: BLE001
                # A broken yaml must not EAT co-arrived triggers (lost wakeup): un-consume
                # every OTHER change (module edits, store writes) so the next tick
                # re-detects them and judges with the OLD spec. The spec's own sig stays
                # advanced — the reload error reports once instead of hammering.
                for p in changed_files:
                    if p != self.spec_path and p in prev_sigs:
                        self._sigs[p] = prev_sigs[p]
                self._store_sig = prev_store_sig
                return self._error_tick(trigger, changed_files, f"spec reload failed: {e}", now)

        produce = (not self.attach) and (first or bool(changed_files))
        if produce and not first:
            # fresh cid per re-run — re-producing a pinned cid double-counts `op: "=="` gates
            self.cid = _new_cid("watch")
        if produce:
            self._evict_sut_modules()
        try:
            run = run_loop(self.spec, cid=self.cid, produce=produce, backend=self._backend())
        except Exception as e:  # noqa: BLE001 — mid-edit import/run crash = transient, not RED
            if produce:
                # The crashed run may have shipped a PARTIAL batch before dying. Absorb
                # its store write so it can't retrigger as `events`, and RETIRE the cid
                # so no later trigger judges a crashed run's partial evidence — a target
                # that ships then dies must never turn COMPLETE (loop forgery, bde2876).
                self._scan_store()
                self.cid = _new_cid("watch")
            return self._error_tick(trigger, changed_files, f"{type(e).__name__}: {e}", now)
        if produce:
            self._scan_store()  # absorb our own store write so it doesn't retrigger next poll
            # watch what the run ACTUALLY imported under root — helper edits must retrigger
            if self.spec.target.mode == "in_process":
                self._helper_paths = sorted(
                    set(_modules_under_root(self.spec.target.root or ".").values()))

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
