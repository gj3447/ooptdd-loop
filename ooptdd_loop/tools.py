"""AI-native tool surface — the loop as introspectable tools for agents.

This is a *pure* registry: each tool is (name, description, parameters, fn) and
``call(name, **args)`` returns a JSON-serializable dict. No MCP runtime is needed
to use or test it — :mod:`ooptdd_loop.mcp_server` is a thin wrapper that exposes
the same registry over MCP so Claude/Codex call these natively (mirrors oo-mcp).

Tools:
  list_requirements(spec)            what a spec declares
  run(spec[, cid])                   run the loop; verdict + next-step context
  verify(cid[, backend])             LTL3 arrival verdict for a cid
  rca(cid[, backend])                log-grounded root-cause context
  ontology_lookup(ontology, event_type)   an EventType's invariants
  coverage(spec_name)                KG: which requirements are DONE (Neo4j)
  drift(spec_name)                   KG: Longinus bindings whose sha256 changed (Neo4j)
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .report import next_step_context
from .spec import load_spec


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # {arg: {"type":..., "required":bool, "desc":...}}
    fn: Callable


def _run_payload(run) -> dict:
    return {
        "cid": run.cid,
        "backend": run.backend,
        "complete": run.complete,
        "done": run.n_done,
        "total": len(run.results),
        "requirements": [
            {"id": r.id, "gate_ok": r.gate_ok, "reachable": r.reachable,
             "bound": r.bound, "done": r.done}
            for r in run.results
        ],
    }


def t_list_requirements(spec: str) -> dict:
    s = load_spec(spec)
    return {"spec": s.name,
            "requirements": [{"id": r.id, "description": r.description, "gate": r.gate}
                             for r in s.requirements]}


def t_run(spec: str, cid: str | None = None) -> dict:
    from .runner import run_loop

    run = run_loop(load_spec(spec), cid=cid)
    payload = _run_payload(run)
    payload["next_step"] = next_step_context(run)   # empty when complete
    return payload


def t_verify(cid: str, backend: str = "memory") -> dict:
    from ooptdd import get_backend, verify_trace

    return verify_trace(get_backend(backend), cid, retries=1)


def t_rca(cid: str, backend: str = "memory") -> dict:
    from ooptdd import get_backend

    from .oo_rca import rca_block

    return {"cid": cid, "rca": rca_block(get_backend(backend), cid, mode=backend, want_events=[])}


def t_ontology_lookup(ontology: str, event_type: str) -> dict:
    from ooptdd.ontology import Ontology

    et = Ontology.from_file(ontology).get(event_type)
    if et is None:
        return {"event_type": event_type, "found": False}
    return {"event_type": et.name, "found": True, "required": et.required,
            "constraints": et.constraints, "description": et.description}


def t_coverage(spec_name: str) -> dict:
    from .kg import Neo4jKgStore

    return Neo4jKgStore().coverage(spec_name)


def t_drift(spec_name: str) -> dict:
    from .kg import Neo4jKgStore

    return {"drift": Neo4jKgStore().drift(spec_name)}


TOOLS: list[Tool] = [
    Tool("list_requirements", "List what a requirements spec declares (ids, descriptions, gates).",
         {"spec": {"type": "string", "required": True, "desc": "path to a spec yaml"}},
         t_list_requirements),
    Tool("run", "Run the positive-TDD loop once; returns the verdict per requirement plus a "
                "log-grounded next-step context (empty when complete).",
         {"spec": {"type": "string", "required": True, "desc": "path to a spec yaml"},
          "cid": {"type": "string", "required": False, "desc": "correlation id (optional)"}},
         t_run),
    Tool("verify", "Three-valued (present/absent/inconclusive) arrival verdict for a cid.",
         {"cid": {"type": "string", "required": True, "desc": "correlation id"},
          "backend": {"type": "string", "required": False, "desc": "backend name (default memory)"}},
         t_verify),
    Tool("rca", "Aggregation-first, log-grounded root-cause context for a cid.",
         {"cid": {"type": "string", "required": True, "desc": "correlation id"},
          "backend": {"type": "string", "required": False, "desc": "backend name (default memory)"}},
         t_rca),
    Tool("ontology_lookup", "Return an event type's invariants (required attrs, constraints).",
         {"ontology": {"type": "string", "required": True, "desc": "path to an ontology yaml"},
          "event_type": {"type": "string", "required": True, "desc": "event type name"}},
         t_ontology_lookup),
    Tool("coverage", "KG query: which requirements are DONE for a spec (latest run). Needs Neo4j.",
         {"spec_name": {"type": "string", "required": True, "desc": "spec name in the KG"}},
         t_coverage),
    Tool("drift", "KG query: Longinus bindings whose source sha256 drifted from baseline. Needs Neo4j.",
         {"spec_name": {"type": "string", "required": True, "desc": "spec name in the KG"}},
         t_drift),
]

REGISTRY = {t.name: t for t in TOOLS}


def list_tools() -> list[dict]:
    """Introspect the surface (name + description + parameter schema)."""
    return [{"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in TOOLS]


def call(name: str, **args) -> dict:
    """Invoke a tool by name. Raises KeyError for an unknown tool."""
    if name not in REGISTRY:
        raise KeyError(f"unknown ooptdd tool {name!r}; have {sorted(REGISTRY)}")
    return REGISTRY[name].fn(**args)
