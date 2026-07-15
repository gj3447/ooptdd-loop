"""The loop — evaluate every requirement, bind Longinus, decide completeness.

One pass:
  1. mint a correlation id for the run
  2. produce logs: call the in-process target callable, or run the target command
     (which ships to the store on its own)
  3. for each requirement: evaluate its gate against the store -> verdict
  4. for each GREEN requirement: verify the Longinus binding to real source
  5. a requirement is DONE iff gate GREEN *and* binding bound
  6. requirements are COMPLETE iff every requirement is DONE

The agent calls this between edits. While anything is RED it gets a log-grounded
RCA (see oo_rca) instead of a guess; it cannot mark a requirement done itself —
the store and the source are the judges.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Sequence

from ooptdd.backends import get_backend

from .engine.selector_gates import evaluate_gate, selector_event_names
from .engine.longinus import ReferenceSite, verify_binding, write_to_kg

if TYPE_CHECKING:
    # charge_coverage 는 adapter(root) 계층 — engine 아님(test_architecture 계층규칙 준수).
    from .charge_coverage import ChargeReport
from .harness import (
    DurableRunJournal,
    JournalEntry,
    LoopGuard,
    LoopReason,
    audit_writeset,
    fix_env,
    git_head,
    kill_process_tree,
)
from .oo_rca import rca_block
from .rules import RuleCheck, evaluate_spec_rules, rule_checks_ok
from .domain.spec import Spec


@dataclass
class ReqResult:
    id: str
    description: str
    gate_ok: bool
    reachable: bool
    checks: list[dict]
    binding: ReferenceSite | None
    rca: str | None = None
    require_binding: bool = False
    waiver: str | None = None
    min_mutation_score: float | None = None
    mutation_score: float | None = None

    @property
    def bound(self) -> bool:
        if self.binding is not None:
            return self.binding.bound
        # No Longinus binding declared. The old default (missing -> bound=True) let
        # done = gate-green + bound-by-OMISSION slip through. Under enforcement, a missing
        # binding is NOT bound unless an explicit `binding_waiver` acknowledges the gap.
        return (not self.require_binding) or bool(self.waiver)

    @property
    def mutation_ok(self) -> bool:
        # A discriminating-power floor: a green gate that a mutant also passes is too weak.
        # Skipped when unconfigured, or when no baseline could be established (score None).
        return (self.min_mutation_score is None or self.mutation_score is None
                or self.mutation_score >= self.min_mutation_score)

    @property
    def done(self) -> bool:
        return self.gate_ok and self.bound and self.mutation_ok


@dataclass
class LoopPass:
    """One iteration of the fixpoint loop: the verdict after a run, plus what the fix
    command did (if anything). The transcript is the audit trail of how the code
    converged — or why it didn't."""

    pass_no: int
    cid: str
    complete: bool
    n_done: int
    total: int
    red: list[str]                  # requirement ids not yet DONE after this pass
    progressed: bool                # did the (done, bound) state change vs the prior pass?
    fix_cmd: str | None = None
    fix_ran: bool = False
    fix_exit: int | None = None
    #: the fix did not return within its bound and was killed (harness.LoopGuard.fix_timeout)
    fix_timed_out: bool = False
    #: this row was replayed from the run journal, not measured in this process. Its fix
    #: fields are unknown by construction: the journal records a pass BEFORE its fix runs.
    resumed: bool = False
    #: S7 write audit of this pass's fix — paths outside the declared allowlist, and why the
    #: audit could not run (either one stops the loop).
    writeset_outside: list[str] = field(default_factory=list)
    writeset_error: str | None = None


@dataclass
class RunResult:
    cid: str
    backend: str
    results: list[ReqResult] = field(default_factory=list)
    methodology_checks: list[RuleCheck] = field(default_factory=list)
    # ②필드 union: main charge + branch transcript/loop_reason (셋 다 default·telemetry, 상호작용 없음).
    # L6 execution-path (charge) coverage — advisory, never affects ``complete``. None on the
    # CI/harness path (logs already produced, nothing measured); a disabled report when the env
    # flag is off or coverage.py is absent; a populated report when measurement ran.
    charge: ChargeReport | None = None
    #: fixpoint-loop audit trail (one entry per pass) and why the loop stopped — a typed
    #: ``harness.LoopReason`` (a ``str`` enum, so it compares and serializes as its value):
    #: ``complete`` | ``max_passes`` | ``stalled`` | ``single_pass`` | ``budget_time`` |
    #: ``budget_spend`` | ``fix_timeout`` | ``writeset_violation``.
    transcript: list[LoopPass] = field(default_factory=list)
    loop_reason: str = "single_pass"
    #: why the loop stopped, in words, when the reason alone is not enough to act on
    #: (which budget blew, what the fix wrote, why an audit could not run).
    loop_note: str | None = None

    @property
    def methodology_ok(self) -> bool:
        return rule_checks_ok(self.methodology_checks)

    @property
    def complete(self) -> bool:
        return (
            bool(self.results)
            and all(r.done for r in self.results)
            and self.methodology_ok
        )

    @property
    def n_done(self) -> int:
        return sum(1 for r in self.results if r.done)


def _want_events(gate: list[dict]) -> list[str]:
    """Event names a gate refers to, across every check shape (event / where /
    must_order). Used to focus the RCA on what the requirement actually expects."""
    want: list[str] = []
    for c in gate:
        if "select" in c or "selector" in c:
            want += [e for e in selector_event_names(c) if e not in want]
        elif "must_order" in c:
            if any(isinstance(e, dict) for e in c["must_order"]):
                want += [e for e in selector_event_names(c) if e not in want]
            else:
                want += [e for e in c["must_order"] if e not in want]
        elif c.get("event") and c["event"] not in want:
            want.append(c["event"])
    return want


def _produce_logs(spec: Spec, backend, cid: str):
    """Run the system under test so it emits events under ``cid``.

    Returns the charge-coverage controller for the run (a no-op one unless ``OOPTDD_CHARGE_COVERAGE``
    is set and coverage.py is installed), so the caller can build the advisory L6 report.
    """
    from .charge_coverage import _NullController, coverage_session

    t = spec.target
    if t.mode == "in_process":
        if not t.callable:
            raise ValueError("in_process target needs `callable: module:function`")
        mod_name, _, fn = t.callable.partition(":")
        import sys

        if t.root and t.root not in sys.path:
            sys.path.insert(0, t.root)
        # Re-import fresh every pass: the fixpoint loop edits the target's source between
        # passes, so a cached module would silently run the OLD code and the loop could
        # never converge (or would report a stale verdict). Dropping it from the import
        # cache forces the edited file to be read.
        importlib.invalidate_caches()
        sys.modules.pop(mod_name, None)
        mod = importlib.import_module(mod_name)
        capture = t.capture or {}
        capture_ctx = nullcontext()
        if capture.get("logging"):
            from .local_capture import capture_logging_to_backend

            capture_ctx = capture_logging_to_backend(
                backend,
                cid,
                logger_name=capture.get("logger") or capture.get("logger_name"),
                level=capture.get("level", "INFO"),
                service=capture.get("service"),
            )
        # Measure the target's entry module while it runs; emit sites elsewhere are out of scope.
        with coverage_session([getattr(mod, "__file__", None)]) as charge:
            with capture_ctx:
                getattr(mod, fn)(backend, cid)
        return charge
    elif t.mode == "command":
        if not t.command:
            raise ValueError("command target needs `command:`")
        env = {**os.environ, "OOPTDD_CID": cid, "OOPTDD_BACKEND": t.backend}
        subprocess.run(t.command, shell=True, env=env, check=False)
        return _NullController(note="command-mode target not measured (runs in a subprocess)")
    else:
        raise ValueError(f"unknown target mode {t.mode!r}")


def _new_cid(prefix: str = "loop") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _load_ontology(spec: Spec):
    ontology = None
    if spec.target.ontology:
        from ooptdd import Ontology  # public API; file-first, offline, no KG dependency

        ontology = Ontology.from_file(os.path.join(spec.target.root, spec.target.ontology))
    return ontology


def evaluate_requirements(spec: Spec, *, cid: str, backend=None, kg_write: bool = False,
                          kg_store=None, charge=None) -> RunResult:
    """Evaluate gates and Longinus bindings for logs that already exist.

    This is the pytest/CI harness path: the test session produced events under
    ``cid``; OOPTDD only judges the arrived evidence and source bindings.
    """
    backend = backend or get_backend(spec.target.backend, **spec.target.backend_options)
    ontology = _load_ontology(spec)

    run = RunResult(
        cid=cid,
        backend=spec.target.backend,
        methodology_checks=evaluate_spec_rules(spec, root=spec.target.root),
    )
    # Enforcement knobs (both opt-in, default OFF). require_binding: a requirement with no
    # Longinus binding is NOT done unless waived. min_mutation: a green gate must also DISCRIMINATE
    # (its mutation score >= the floor) — rewards strong gates, not green theater.
    require_binding = bool(os.getenv("OOPTDD_REQUIRE_BINDING")) or spec.methodology.enforce
    _mm = os.getenv("OOPTDD_MIN_MUTATION_SCORE")
    min_mutation = float(_mm) if _mm else None
    for req in spec.requirements:
        gate_spec = {"cid": cid, "expect": req.gate}
        ev = evaluate_gate(backend, gate_spec, ontology=ontology)
        binding = (
            verify_binding(spec.target.root, req.longinus) if req.longinus else None
        )
        mutation_score = None
        if min_mutation is not None and ev["ok"] and req.gate:
            try:  # mutation is an optional discriminating-power gate — never crash the loop
                from ooptdd.mutation import mutation_report

                from .engine.selector_gates import _query_events
                rep = mutation_report(_query_events(backend, cid).events, gate_spec)
                mutation_score = rep["score"] if rep.get("baseline_green") else None
            except Exception:  # noqa: BLE001
                mutation_score = None
        rr = ReqResult(
            id=req.id, description=req.description, gate_ok=ev["ok"],
            reachable=ev["reachable"], checks=ev["checks"], binding=binding,
            require_binding=require_binding, waiver=req.extras.get("binding_waiver"),
            min_mutation_score=min_mutation, mutation_score=mutation_score,
        )
        if not rr.done:
            rr.rca = rca_block(backend, cid, mode=spec.target.backend,
                               want_events=_want_events(req.gate))
        elif kg_write and binding is not None:
            write_to_kg(binding, cycle_id=cid)
        run.results.append(rr)
    if charge is not None:
        # L6 advisory: which executed emit sites never reached the store. Pure reporting —
        # build it from the full arrived trace, independent of any single gate's shape.
        from .charge_coverage import build_charge_report

        from .engine.selector_gates import _query_events
        try:
            observed = _query_events(backend, cid).events
        except Exception:  # noqa: BLE001 — the advisory must never break evaluation
            observed = []
        run.charge = build_charge_report(charge, observed)
    if kg_store is not None:
        # KG-native I/O: persist the run so coverage/drift become queries (V2)
        kg_store.write_run(cid, spec.name, run.results)
    return run


def run_loop(spec: Spec, *, cid: str | None = None, kg_write: bool = False,
             kg_store=None, produce: bool = True, backend=None) -> RunResult:
    cid = cid or os.getenv("OOPTDD_CID") or _new_cid()
    # ``backend`` lets a long-lived caller (watch) inject a wrapped store — e.g. a query
    # window pinned to its session start. Default (None) is byte-identical to before.
    backend = backend or get_backend(spec.target.backend, **spec.target.backend_options)
    charge = None
    if produce:
        # ``produce=False`` re-evaluates already-shipped logs without re-running the system
        # under test — used by run_until_complete for every pass after the first, so a stable
        # cid is not re-shipped (see there).
        charge = _produce_logs(spec, backend, cid)
    return evaluate_requirements(
        spec,
        cid=cid,
        backend=backend,
        kg_write=kg_write,
        kg_store=kg_store,
        charge=charge,
    )


def _loop_state(run: RunResult) -> str:
    """A canonical snapshot of progress — the (id, done, bound) of every requirement plus
    the methodology verdict. Two passes with the same state made no progress, which is how
    the loop detects a stall (an agent editing in circles) instead of spinning forever.

    Serialized rather than hashed so the same key survives a crash in the run journal (S4):
    a resumed loop compares this pass against the last one the *previous* process recorded.
    """
    return json.dumps(
        {
            "reqs": sorted([r.id, r.done, r.bound] for r in run.results),
            "methodology_ok": run.methodology_ok,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _red_ids(run: RunResult) -> list[str]:
    return [r.id for r in run.results if not r.done]


def _run_fix(fix_cmd: str, spec: Spec, cid: str, rca: str, *,
             env_allowlist: str | Sequence[str] | None = None,
             timeout: float | None = None) -> int:
    """Run the fix command so it can mutate the code from the RCA, then return its exit
    code. The RCA is passed both on stdin and via ``OOPTDD_RCA``/``OOPTDD_CID``/``OOPTDD_ROOT``
    so the command (a script, or an agent invocation like ``claude -p "$OOPTDD_RCA"``) has
    the grounded evidence. The loop only *invokes* the generator; it never imports or
    hardcodes one — the generator≠verifier boundary the methodology depends on.

    Contained (S7) and bounded (S5): the environment is an explicit allowlist rather than
    everything the parent holds (see ``harness.fix_env`` — this is a behavior break), and a
    fix that does not return within ``timeout`` has its whole process tree killed and
    raises ``subprocess.TimeoutExpired`` for the loop to turn into a typed stop. The fix
    gets its own session so killing it takes the agent it spawned with it.
    """
    env = fix_env(
        {
            "OOPTDD_CID": cid,
            "OOPTDD_RCA": rca,
            "OOPTDD_ROOT": spec.target.root,
            "OOPTDD_BACKEND": spec.target.backend,
        },
        allowlist=env_allowlist,
    )
    proc = subprocess.Popen(fix_cmd, shell=True, env=env, stdin=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        proc.communicate(input=rca, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        proc.communicate()          # reap the killed tree so it cannot become a zombie
        raise
    return proc.returncode


def _replayed_pass(entry: JournalEntry) -> LoopPass:
    """Rebuild a transcript row from the journal so a resumed run reports the whole run,
    not just the passes this process happened to make (S4)."""
    return LoopPass(
        pass_no=entry.pass_no, cid=entry.cid, complete=entry.complete, n_done=entry.n_done,
        total=entry.total, red=list(entry.red), progressed=entry.progressed, resumed=True,
    )


def run_until_complete(spec: Spec, *, cid: str | None = None, max_passes: int = 1,
                       kg_write: bool = False, kg_store=None,
                       fix_cmd: str | None = None, patience: int = 2,
                       backoff_s: float = 0.0,
                       max_seconds: float | None = None,
                       max_spend: float | None = None,
                       spend_fn: Callable[[], float] | None = None,
                       fix_timeout_s: float | None = None,
                       journal_path=None, run_id: str | None = None, resume: bool = False,
                       env_allowlist: str | Sequence[str] | None = None,
                       write_allowlist: Sequence[str] | None = None):
    """Drive the requirements loop to a fixpoint: run → (if RED) fix → re-run, until every
    requirement is DONE, a budget is spent, the loop stalls, or the fix breaks its contract.

    Two pass regimes on one budget (③ 병합: 브랜치 fixpoint superset + main 재조회 보존):
      * FIX PRESENT — genuine edit-run fixpoint. The code changes between passes, so the SUT is
        RE-RUN every pass under a FRESH cid: a clean measurement of the current code, never
        accumulating a prior pass's events into an ``op: ==`` gate. The fix command (``fix_cmd``
        arg, else ``spec.target.fix``) — a shell command, typically an agent — edits the code
        from the log-grounded RCA between RED passes.
      * NO FIX — historical re-query (main's behavior, preserved). The code does not change, so
        the SUT runs ONCE (pass 1) and later passes only RE-QUERY the pinned cid to absorb
        async-ingest latency; re-producing under a stable cid would double-count and flip
        exact-count gates. ``max_passes=1`` (default) is a single pass — fully backward compatible.

    Every bound lives in ``harness.LoopGuard``, and every stop the loop DECIDES is a typed
    ``harness.LoopReason`` (S1): no budget here is unbounded, and the failure modes the loop
    owns — a spent budget, a hung fix, an unreadable meter, an unauditable write-set — are
    typed stops rather than tracebacks. That is not a blanket "no exception escapes". Two
    kinds deliberately do: a MISCONFIGURATION raises (``ValueError`` below, or
    ``JournalCorruptionError``/``OSError`` from the journal), which ``cli`` turns into a
    clean exit 2 rather than running on with a guard silently disabled; and an exception
    from the SYSTEM UNDER TEST propagates to the caller, since the loop has no verdict to
    report about code that could not run.

    * ``patience`` consecutive no-progress passes = a stall (an agent editing in circles).
    * ``max_seconds`` (S5) bounds the wall-clock — measured from THIS call, so resuming grants
      a fresh window — and stops with ``budget_time``. It also bounds each fix invocation, so a
      hung agent can never make the between-pass checks unreachable.
    * ``max_spend`` + ``spend_fn`` (S5) stop with ``budget_spend``; a spend meter that cannot
      be read stops the loop fail-closed. ``max_spend`` without ``spend_fn`` is a ``ValueError``
      here, not a silent no-op.
    * ``fix_timeout_s`` (S5) bounds one fix invocation independently of the wall-clock; the
      effective bound is the tighter of the two. On timeout the fix's process tree is killed and
      the loop stops with ``fix_timeout``.
    * ``journal_path`` (S4) appends one JSONL line per completed pass; ``resume=True`` with the
      same ``run_id`` restarts at the next unpaid pass with the recorded stall state, instead
      of repaying every agent call from pass 1. ``run_id`` defaults to the cid, which itself
      defaults to a fresh UUID — so ``resume=True`` REQUIRES a caller-supplied ``run_id`` or
      ``cid`` (or ``$OOPTDD_CID``) and is a ``ValueError`` without one: a resume keyed on an
      identity the loop just invented matches no journal line and would silently repay from
      pass 1. That catches a MISSING identity, not a WRONG one — a mistyped ``run_id`` also
      matches nothing and also restarts at pass 1, and the loop cannot tell that apart from a
      run that crashed before its first pass completed. ``LoopPass.resumed`` is the tell.
    * ``env_allowlist`` (S7) is the fix command's environment. **Default is a scrub, which is a
      behavior break** — see ``harness.fix_env`` for the migration path (``harness.INHERIT_ALL``).
      It is LITERAL: it replaces ``harness.DEFAULT_ENV_ALLOWLIST`` rather than extending it, so
      pass ``[*harness.DEFAULT_ENV_ALLOWLIST, "ANTHROPIC_API_KEY"]``, not ``["ANTHROPIC_API_KEY"]``
      (which leaves the fix no PATH). The CLI's ``--fix-env-allow`` is the additive form.
    * ``write_allowlist`` (S7) declares the path prefixes the fix may write to; anything else
      git can see, and any audit that cannot run, stops with ``writeset_violation``. Read
      ``harness.audit_writeset`` for exactly what that does and does not cover.

    The ``charge`` (L6) report from the last produce pass rides onto the final RunResult, since
    it must describe the code the final verdict judges.
    """
    from .report import next_step_context

    fix = fix_cmd if fix_cmd is not None else spec.target.fix
    guard = LoopGuard(max_passes=max_passes, patience=patience, max_seconds=max_seconds,
                      max_spend=max_spend, spend_fn=spend_fn, fix_timeout_s=fix_timeout_s)
    cid = cid or os.getenv("OOPTDD_CID")
    if resume:
        if journal_path is None:
            raise ValueError("resume=True needs journal_path — there is nothing to resume from")
        if run_id is None and cid is None:
            # run_id defaults to the cid, and an unsupplied cid is a fresh UUID minted right
            # below. Resuming on that identity matches no journal line, so the loop would
            # start at pass 1 and repay every agent call — the exact failure the journal
            # exists to prevent, silently. Same precedent as max_spend without spend_fn: a
            # guard that can never fire is worse than no guard.
            raise ValueError(
                "resume=True needs a stable run_id: a resume keyed on a freshly generated "
                "cid can never match a journal line and would silently repay the agent "
                "from pass 1"
            )
    cid = cid or _new_cid()
    run_id = run_id or cid
    journal = DurableRunJournal(journal_path, run_id) if journal_path else None
    transcript: list[LoopPass] = []
    prev_state = None
    start_pass = 0
    if resume:
        replay = journal.replay()
        transcript = [_replayed_pass(e) for e in replay.entries]
        start_pass = replay.passes          # the passes already paid for; do not repay them
        guard.stall = replay.stall
        prev_state = replay.state_key
    guard.start()
    last: RunResult | None = None
    last_charge = None
    reason: LoopReason | None = None

    for i in range(start_pass + 1, guard.budget + 1):
        if last is not None:
            # between passes only: the first pass of this call always gets to measure, so
            # the loop can never return a verdict it did not take.
            reason = guard.resource_stop()
            if reason:
                break
        started_at = time.time()
        if fix:
            # genuine fixpoint: re-run the (possibly edited) code every pass under a fresh cid.
            last = run_loop(spec, cid=_new_cid(), kg_write=kg_write, kg_store=kg_store, produce=True)
        else:
            # historical re-query: produce once (pass 1), then re-query the pinned cid.
            last = run_loop(spec, cid=cid, kg_write=kg_write, kg_store=kg_store, produce=(i == 1))
        cid = last.cid
        if last.charge is not None:
            last_charge = last.charge   # carry the last measured charge onto the final result
        state = _loop_state(last)
        progressed = prev_state is None or state != prev_state
        prev_state = state
        guard.note_progress(progressed, pass_no=i)
        record = LoopPass(
            pass_no=i, cid=last.cid, complete=last.complete, n_done=last.n_done,
            total=len(last.results), red=_red_ids(last), progressed=progressed,
        )
        transcript.append(record)
        if journal is not None:
            # durable BEFORE the fix runs: a crash inside the fix then resumes at the NEXT
            # pass and re-measures the code on disk, rather than re-paying for that edit.
            journal.append(JournalEntry(
                run_id=run_id, pass_no=i, cid=last.cid, complete=last.complete,
                n_done=last.n_done, total=len(last.results), red=tuple(record.red),
                gates=tuple({"id": r.id, "gate_ok": r.gate_ok, "bound": r.bound, "done": r.done}
                            for r in last.results),
                progressed=progressed, stall=guard.stall, state_key=state,
                started_at=started_at, ended_at=time.time(),
            ))

        if last.complete:
            reason = LoopReason.COMPLETE
            break
        reason = guard.step_stop(i)              # step ceiling, then the no-progress stall
        if reason:
            break
        reason = guard.resource_stop()           # never pay an agent past a spent budget
        if reason:
            break
        if fix:
            record.fix_cmd = fix
            record.fix_ran = True
            pre_head = git_head(spec.target.root) if write_allowlist is not None else None
            try:
                record.fix_exit = _run_fix(fix, spec, last.cid, next_step_context(last),
                                           env_allowlist=env_allowlist,
                                           timeout=guard.fix_timeout())
            except subprocess.TimeoutExpired as exc:
                record.fix_timed_out = True
                guard.stop_note = f"fix command exceeded {exc.timeout:.2f}s and was killed"
                reason = LoopReason.FIX_TIMEOUT
                break
            if write_allowlist is not None:
                audit = audit_writeset(spec.target.root, write_allowlist, pre_head=pre_head)
                record.writeset_outside = list(audit.outside)
                record.writeset_error = audit.error
                if not audit.ok:
                    guard.stop_note = audit.summary()
                    reason = LoopReason.WRITESET_VIOLATION
                    break
        if backoff_s:
            time.sleep(backoff_s)

    if last is None:
        # Resumed at/after the pass budget: no pass ran here. There is no verdict to report
        # yet, and an empty re-query of an unproduced cid would MANUFACTURE an all-RED one.
        # Take the genuine measurement this spec's own regime takes — fresh cid + produce for
        # a fix loop (what its passes do), produce on the pinned cid otherwise (its pass 1).
        last = run_loop(spec, cid=(_new_cid() if fix else cid), kg_write=kg_write,
                        kg_store=kg_store, produce=True)
        if last.charge is not None:
            last_charge = last.charge
        reason = LoopReason.COMPLETE if last.complete else (
            LoopReason.SINGLE_PASS if guard.budget == 1 else LoopReason.MAX_PASSES)
        guard.stop_note = (f"resumed past the {guard.budget}-pass budget "
                           f"({start_pass} pass(es) already journaled); "
                           "the verdict is a fresh measurement of the code on disk")

    if reason is None:  # unreachable: step_stop always fires at the ceiling. Belt and braces.
        reason = LoopReason.SINGLE_PASS if guard.budget == 1 else LoopReason.MAX_PASSES
    last.transcript = transcript
    last.loop_reason = reason
    last.loop_note = guard.stop_note
    # MODE B 의 마지막 pass 는 re-query(produce=False)라 charge=None — 코드 불변이므로 pass1 의 charge 를 실어준다.
    if last.charge is None and last_charge is not None:
        last.charge = last_charge
    return last
