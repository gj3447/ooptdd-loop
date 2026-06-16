"""``ooptdd-loop`` — run the requirements loop once.

    ooptdd-loop run <spec.yaml> [--cid CID] [--kg-write] [--json] [--passes N]

Exit 0 = all requirements DONE (gate GREEN + Longinus bound); 1 = incomplete.
On incomplete it prints the log-grounded next-step context for the agent.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .report import next_step_context, render
from .runner import run_until_complete
from .spec import load_spec


def _neo4j_store():
    from .kg import Neo4jKgStore  # env-gated; raises if NEO4J_* / driver absent
    return Neo4jKgStore()


def _cmd_run(args) -> int:
    spec = load_spec(args.spec)
    store = _neo4j_store() if args.kg else None
    run = run_until_complete(spec, cid=args.cid, max_passes=args.passes,
                             kg_write=args.kg_write, kg_store=store)
    if args.json:
        payload = {
            "cid": run.cid,
            "backend": run.backend,
            "complete": run.complete,
            "done": run.n_done,
            "total": len(run.results),
            "requirements": [
                {
                    "id": r.id,
                    "gate_ok": r.gate_ok,
                    "reachable": r.reachable,
                    "bound": r.bound,
                    "done": r.done,
                    "checks": r.checks,
                    "binding": dataclasses.asdict(r.binding) if r.binding else None,
                }
                for r in run.results
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(run))
        ctx = next_step_context(run)
        if ctx:
            print("\n" + ctx, file=sys.stderr)
    return 0 if run.complete else 1


def _cmd_coverage(args) -> int:
    cov = _neo4j_store().coverage(args.spec_name)
    print(json.dumps(cov, ensure_ascii=False, indent=2))
    return 0 if cov.get("total", 0) and cov.get("complete") else 1


def _cmd_drift(args) -> int:
    d = _neo4j_store().drift(args.spec_name)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if d:
        print(f"DRIFT — {len(d)} Longinus binding(s) changed vs baseline", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ooptdd-loop")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the requirements loop once")
    r.add_argument("spec")
    r.add_argument("--cid")
    r.add_argument("--kg-write", action="store_true", help="write Longinus ReferenceSites to KG")
    r.add_argument("--kg", action="store_true", help="persist the run (verdicts+sites) to Neo4j")
    r.add_argument("--json", action="store_true")
    r.add_argument("--passes", type=int, default=1)
    r.set_defaults(func=_cmd_run)

    c = sub.add_parser("coverage", help="query KG: which requirements are DONE (latest run)")
    c.add_argument("spec_name")
    c.set_defaults(func=_cmd_coverage)

    d = sub.add_parser("drift", help="query KG: Longinus bindings whose source sha256 changed")
    d.add_argument("spec_name")
    d.set_defaults(func=_cmd_drift)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
