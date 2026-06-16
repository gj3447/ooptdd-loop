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
from dataclasses import dataclass, field

from ooptdd.backends import get_backend
from ooptdd.gate import evaluate

from .longinus import ReferenceSite, verify_binding, write_to_kg
from .oo_rca import rca_block
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

    @property
    def bound(self) -> bool:
        return self.binding is None or self.binding.bound

    @property
    def done(self) -> bool:
        return self.gate_ok and self.bound


@dataclass
class RunResult:
    cid: str
    backend: str
    results: list[ReqResult] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.results) and all(r.done for r in self.results)

    @property
    def n_done(self) -> int:
        return sum(1 for r in self.results if r.done)


def _produce_logs(spec: Spec, backend, cid: str) -> None:
    """Run the system under test so it emits events under ``cid``."""
    t = spec.target
    if t.mode == "in_process":
        if not t.callable:
            raise ValueError("in_process target needs `callable: module:function`")
        mod_name, _, fn = t.callable.partition(":")
        import sys

        if t.root and t.root not in sys.path:
            sys.path.insert(0, t.root)
        mod = importlib.import_module(mod_name)
        getattr(mod, fn)(backend, cid)
    elif t.mode == "command":
        if not t.command:
            raise ValueError("command target needs `command:`")
        env = {**os.environ, "OOPTDD_CID": cid, "OOPTDD_BACKEND": t.backend}
        subprocess.run(t.command, shell=True, env=env, check=False)
    else:
        raise ValueError(f"unknown target mode {t.mode!r}")


def run_loop(spec: Spec, *, cid: str | None = None, kg_write: bool = False) -> RunResult:
    cid = cid or os.getenv("OOPTDD_CID") or f"loop-{uuid.uuid4().hex[:12]}"
    backend = get_backend(spec.target.backend, **spec.target.backend_options)

    _produce_logs(spec, backend, cid)

    run = RunResult(cid=cid, backend=spec.target.backend)
    for req in spec.requirements:
        gate_spec = {"cid": cid, "expect": req.gate}
        ev = evaluate(backend, gate_spec)
        binding = (
            verify_binding(spec.target.root, req.longinus) if req.longinus else None
        )
        rr = ReqResult(
            id=req.id, description=req.description, gate_ok=ev["ok"],
            reachable=ev["reachable"], checks=ev["checks"], binding=binding,
        )
        if not rr.done:
            want = [c["event"] for c in req.gate]
            rr.rca = rca_block(backend, cid, mode=spec.target.backend, want_events=want)
        elif kg_write and binding is not None:
            write_to_kg(binding, cycle_id=cid)
        run.results.append(rr)
    return run


def run_until_complete(spec: Spec, *, cid: str | None = None, max_passes: int = 1,
                       kg_write: bool = False):
    """Run the loop up to ``max_passes`` times (the code does not change between
    passes — re-runs only help with async-ingest latency on networked backends).
    Returns the final RunResult."""
    last = None
    for _ in range(max(max_passes, 1)):
        last = run_loop(spec, cid=cid, kg_write=kg_write)
        if last.complete:
            break
        time.sleep(0.0)
    return last
