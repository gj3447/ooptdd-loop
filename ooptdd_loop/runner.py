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
import os
import subprocess
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ooptdd.backends import get_backend

from .selector_gates import evaluate_gate, selector_event_names

if TYPE_CHECKING:
    from .charge_coverage import ChargeReport
from .longinus import ReferenceSite, verify_binding, write_to_kg
from .oo_rca import rca_block
from .rules import RuleCheck, evaluate_spec_rules, rule_checks_ok
from .spec import Spec


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
class RunResult:
    cid: str
    backend: str
    results: list[ReqResult] = field(default_factory=list)
    methodology_checks: list[RuleCheck] = field(default_factory=list)
    # L6 execution-path (charge) coverage — advisory, never affects ``complete``. None on the
    # CI/harness path (logs already produced, nothing measured); a disabled report when the env
    # flag is off or coverage.py is absent; a populated report when measurement ran.
    charge: ChargeReport | None = None

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
        from ooptdd.ontology import Ontology  # file-first; offline, no KG dependency

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
        methodology_checks=evaluate_spec_rules(spec),
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

                from .selector_gates import _query_events
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

        from .selector_gates import _query_events
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
             kg_store=None, produce: bool = True) -> RunResult:
    cid = cid or os.getenv("OOPTDD_CID") or _new_cid()
    backend = get_backend(spec.target.backend, **spec.target.backend_options)
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


def run_until_complete(spec: Spec, *, cid: str | None = None, max_passes: int = 1,
                       kg_write: bool = False, kg_store=None):
    """Run the loop up to ``max_passes`` times (the code does not change between passes).

    The system under test is run **once**; the extra passes only RE-QUERY the store, to give a
    networked backend time for async ingest to land. Re-running the target every pass would
    re-ship every event — and against an in-process store with a stable cid that doubles the
    counts and flips exact-count gates (``op: ==``) from GREEN to RED on correct code. The cid
    is therefore minted once here and held fixed across passes (a fresh cid per pass would make
    the no-produce passes query an empty stream). Returns the final RunResult."""
    cid = cid or os.getenv("OOPTDD_CID") or _new_cid()
    last = None
    for i in range(max(max_passes, 1)):
        last = run_loop(spec, cid=cid, kg_write=kg_write, kg_store=kg_store, produce=(i == 0))
        if last.complete:
            break
        time.sleep(0.0)
    return last
