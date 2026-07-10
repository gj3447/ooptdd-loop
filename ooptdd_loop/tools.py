# KG: OOPTDD_methodology_v1
"""AI-native tool surface — the loop as introspectable tools for agents.

This is a *pure* registry: each tool is (name, description, parameters, fn) and
``call(name, **args)`` returns a JSON-serializable dict. No MCP runtime is needed
to use or test it — :mod:`ooptdd_loop.mcp_server` is a thin wrapper that exposes
the same registry over MCP so Claude/Codex call these natively (mirrors oo-mcp).

Tools:
  list_requirements(spec)            what a spec declares
  run(spec[, cid])                   run the loop; verdict + next-step context
  watch_tick(spec, cid[, produce])   one-shot incremental re-judgment of a cid
  validate_spec(spec)                static OOPTDD methodology checks, no execution
  methodology_rules()                canonical OOPTDD rules
  kg_seed()                          idempotent KG seed Cypher + params
  harness_profile()                  L_IDE/L_RT/L_MC integration map
  verify(cid[, backend])             LTL3 arrival verdict for a cid
  rca(cid[, backend])                log-grounded root-cause context
  logserver_tools()                  upstream log MCP tool discovery
  logserver_health([stale_minutes])  upstream log MCP ingest freshness
  logserver_trace(cid[, minutes_back])  upstream log MCP cross-stream timeline
  logserver_query(sql[, minutes_back, size])  upstream log MCP SQL query
  logserver_errors([minutes_back, stream])    upstream log MCP recent errors
  golden_save(spec, out[, cid, run])     save deterministic golden baseline
  golden_diff(spec, baseline[, cid, run]) compare current run to golden baseline
  ontology_lookup(ontology, event_type)   an EventType's invariants
  coverage(spec_name)                KG: which requirements are DONE (Neo4j)
  drift(spec_name)                   KG: Longinus bindings whose sha256 changed (Neo4j)
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from .report import next_step_context
from .rules import (
    OOPTDD_METHOD_NAME,
    evaluate_spec_rules,
    is_ooptdd_enabled,
    rule_checks_ok,
    rules_as_dicts,
)
from .domain.spec import load_spec


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
        "methodology_ok": run.methodology_ok,
        "methodology_checks": [asdict(c) for c in run.methodology_checks],
        "requirements": [
            {"id": r.id, "gate_ok": r.gate_ok, "reachable": r.reachable,
             "bound": r.bound, "done": r.done}
            for r in run.results
        ],
    }


def t_list_requirements(spec: str) -> dict:
    s = load_spec(spec)
    return {
        "spec": s.name,
        "methodology": {"name": s.methodology.name, "enforce": s.methodology.enforce},
        "contracts": [
            {
                "id": c.id,
                "kind": c.kind,
                "status": c.status,
                "message": c.message,
                "source_req": c.source_req,
                "integration_backstop": c.integration_backstop,
            }
            for c in s.contracts
        ],
        "requirements": [
            {
                "id": r.id,
                "kind": r.kind,
                "description": r.description,
                "gate": r.gate,
                "covers": r.covers,
                "longinus": asdict(r.longinus) if r.longinus else None,
            }
            for r in s.requirements
        ],
    }


def t_run(spec: str, cid: str | None = None) -> dict:
    from .runner import run_loop

    run = run_loop(load_spec(spec), cid=cid)
    payload = _run_payload(run)
    payload["next_step"] = next_step_context(run)   # empty when complete
    return payload


def t_watch_tick(spec: str, cid: str, produce: bool = False) -> dict:
    """One-shot watch tick — the MCP-safe (non-looping) form of ``ooptdd-loop watch``.

    MCP is synchronous request/response, so the infinite watch loop cannot be a tool;
    this re-judges the events already shipped under ``cid`` (``produce=False``, the
    ``--attach`` semantic: call again as more events arrive) or runs the target first
    under that cid (``produce=True``). kg_write/kg_store stay off — repeat-safe."""
    from .runner import run_loop
    from .watch import run_payload

    run = run_loop(load_spec(spec), cid=cid, produce=produce)
    payload = run_payload(run)
    payload["next_step"] = next_step_context(run)   # empty when complete
    return payload


def t_validate_spec(spec: str) -> dict:
    s = load_spec(spec)
    checks = evaluate_spec_rules(s, root=s.target.root)
    return {
        "spec": s.name,
        "methodology": {"name": s.methodology.name, "enforce": s.methodology.enforce},
        "ooptdd_enabled": is_ooptdd_enabled(s),
        "ok": rule_checks_ok(checks),
        "requirements": len(s.requirements),
        "contracts": len(s.contracts),
        "checks": [asdict(c) for c in checks],
    }


def t_methodology_rules() -> dict:
    return {"methodology": OOPTDD_METHOD_NAME, "rules": rules_as_dicts()}


def t_kg_seed() -> dict:
    from .kg_seed import seed_cypher, seed_payload

    return {"cypher": seed_cypher(), "params": seed_payload()}


def t_harness_profile() -> dict:
    return {
        "family": "harness",
        "layers": {
            "L_IDE": {
                "purpose": "local coding harness controls",
                "surfaces": [
                    "ooptdd-loop run",
                    "pytest --ooptdd-spec",
                    "target.capture.logging",
                    "ooptdd-loop validate-spec",
                    "pytest",
                    "next_step_context",
                ],
                "axes": {
                    "Inform": ["list_requirements", "methodology_rules"],
                    "Constrain": ["validate_spec", "methodology_checks"],
                    "Verify": [
                        "run",
                        "verify",
                        "ontology_lookup",
                        "logserver_trace",
                        "logserver_query",
                        "golden_diff",
                        "target.capture.logging",
                    ],
                    "Correct": ["rca", "next_step", "logserver_errors", "golden_save"],
                },
            },
            "L_RT": {
                "purpose": "agent runtime tool surface",
                "surfaces": [
                    "ooptdd-loop-mcp",
                    "ooptdd_loop.tools",
                    "pytest11:ooptdd_loop",
                    "oo-mcp upstream",
                ],
                "tools": [t.name for t in TOOLS],
            },
            "L_MC": {
                "purpose": "managed/control-plane evidence",
                "surfaces": [
                    "kg_seed",
                    "coverage",
                    "drift",
                    "Longinus ReferenceSite",
                    "golden_save",
                    "golden_diff",
                    "logserver_health",
                    "logserver_trace",
                ],
                "stores": ["Neo4j", "OpenObserve/oo", "memory"],
            },
        },
    }


def t_verify(cid: str, backend: str = "memory") -> dict:
    from ooptdd import get_backend, verify_trace

    return verify_trace(get_backend(backend), cid, retries=1)


def t_rca(cid: str, backend: str = "memory") -> dict:
    from ooptdd import get_backend

    from .oo_rca import rca_block

    return {"cid": cid, "rca": rca_block(get_backend(backend), cid, mode=backend, want_events=[])}


def t_logserver_tools() -> dict:
    from .log_mcp import safe_list_log_tools

    return safe_list_log_tools()


def t_logserver_health(stale_minutes: float = 15.0) -> dict:
    from .log_mcp import logserver_health

    return logserver_health(stale_minutes)


def t_logserver_trace(cid: str, minutes_back: float = 60.0) -> dict:
    from .log_mcp import logserver_trace

    return logserver_trace(cid, minutes_back)


def t_logserver_query(sql: str, minutes_back: float = 60.0, size: int = 100) -> dict:
    from .log_mcp import logserver_query

    return logserver_query(sql, minutes_back, size)


def t_logserver_errors(minutes_back: float = 60.0, stream: str | None = None) -> dict:
    from .log_mcp import logserver_errors

    return logserver_errors(minutes_back, stream=stream)


def t_golden_save(
    spec: str,
    out: str,
    cid: str | None = None,
    run: bool = False,
    allow_incomplete: bool = False,
) -> dict:
    from .golden import save_golden

    return save_golden(
        load_spec(spec),
        out=out,
        cid=cid,
        run=run,
        allow_incomplete=allow_incomplete,
    )


def t_golden_diff(
    spec: str,
    baseline: str,
    cid: str | None = None,
    run: bool = False,
) -> dict:
    from .golden import diff_golden

    return diff_golden(load_spec(spec), baseline=baseline, cid=cid, run=run)


def t_ontology_lookup(ontology: str, event_type: str) -> dict:
    from ooptdd import Ontology  # public API

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
    Tool("watch_tick", "One-shot watch tick: re-judge the events already shipped under a cid "
                       "(produce=false, incremental — call again as events arrive), or run the "
                       "target first under that cid (produce=true). Non-looping MCP form of "
                       "`ooptdd-loop watch`.",
         {"spec": {"type": "string", "required": True, "desc": "path to a spec yaml"},
          "cid": {"type": "string", "required": True, "desc": "correlation id to (re-)judge"},
          "produce": {"type": "boolean", "required": False,
                      "desc": "run the target under this cid before judging (default false)"}},
         t_watch_tick),
    Tool("validate_spec", "Validate OOPTDD methodology rules without running the system.",
         {"spec": {"type": "string", "required": True, "desc": "path to a spec yaml"}},
         t_validate_spec),
    Tool("methodology_rules", "Return the canonical OOPTDD rule ontology.",
         {}, t_methodology_rules),
    Tool("kg_seed", "Return idempotent Cypher and params for seeding OOPTDD rules into KG.",
         {}, t_kg_seed),
    Tool("harness_profile", "Return how ooptdd-loop maps to L_IDE/L_RT/L_MC harness layers.",
         {}, t_harness_profile),
    Tool("verify", "Three-valued (present/absent/inconclusive) arrival verdict for a cid.",
         {"cid": {"type": "string", "required": True, "desc": "correlation id"},
          "backend": {"type": "string", "required": False, "desc": "backend name (default memory)"}},
         t_verify),
    Tool("rca", "Aggregation-first, log-grounded root-cause context for a cid.",
         {"cid": {"type": "string", "required": True, "desc": "correlation id"},
          "backend": {"type": "string", "required": False, "desc": "backend name (default memory)"}},
         t_rca),
    Tool("logserver_tools", "List tools exposed by the upstream log-server MCP endpoint.",
         {}, t_logserver_tools),
    Tool("logserver_health", "MCP log server: ingest freshness by stream.",
         {"stale_minutes": {"type": "number", "required": False,
                            "desc": "staleness threshold in minutes (default 15)"}},
         t_logserver_health),
    Tool("logserver_trace", "MCP log server: cross-stream timeline for a correlation/cycle id.",
         {"cid": {"type": "string", "required": True, "desc": "correlation/cycle id"},
          "minutes_back": {"type": "number", "required": False,
                           "desc": "lookback window in minutes (default 60)"}},
         t_logserver_trace),
    Tool("logserver_query", "MCP log server: run SQL over logs.",
         {"sql": {"type": "string", "required": True, "desc": "OpenObserve SQL"},
          "minutes_back": {"type": "number", "required": False,
                           "desc": "lookback window in minutes (default 60)"},
          "size": {"type": "integer", "required": False,
                   "desc": "maximum records (default 100)"}},
         t_logserver_query),
    Tool("logserver_errors", "MCP log server: recent ERROR/CRITICAL records.",
         {"minutes_back": {"type": "number", "required": False,
                           "desc": "lookback window in minutes (default 60)"},
          "stream": {"type": "string", "required": False,
                     "desc": "optional stream filter"}},
         t_logserver_errors),
    Tool("golden_save", "Save a deterministic golden baseline for a spec/cid.",
         {"spec": {"type": "string", "required": True, "desc": "path to a spec yaml"},
          "out": {"type": "string", "required": True, "desc": "baseline JSON path"},
          "cid": {"type": "string", "required": False, "desc": "correlation id"},
          "run": {"type": "boolean", "required": False,
                  "desc": "run the spec before saving"},
          "allow_incomplete": {"type": "boolean", "required": False,
                               "desc": "allow saving an incomplete run"}},
         t_golden_save),
    Tool("golden_diff", "Compare a current run against a golden baseline.",
         {"spec": {"type": "string", "required": True, "desc": "path to a spec yaml"},
          "baseline": {"type": "string", "required": True, "desc": "baseline JSON path"},
          "cid": {"type": "string", "required": False, "desc": "correlation id"},
          "run": {"type": "boolean", "required": False,
                  "desc": "run the spec before diffing"}},
         t_golden_diff),
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
