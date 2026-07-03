"""Engine layer — the binding/gate logic the loop orchestrates.

  - :mod:`ooptdd_loop.engine.longinus`        AST call-graph binding + runtime reachability
  - :mod:`ooptdd_loop.engine.selector_gates`  selector/trace-gate evaluation over ooptdd

Dependency rule: the engine imports the domain (spec model) and the upstream ``ooptdd``
public API, never a loop adapter (runner, kg, log_mcp, cli, …). It stays pure logic so the
application layer can wire it to any store/agent. The architecture fitness test enforces it.
"""
from .longinus import (
    ReferenceSite,
    emission_kind,
    git_identity,
    load_coverage,
    verify_binding,
    write_to_kg,
)
from .selector_gates import evaluate_gate, selector_event_names

__all__ = [
    "ReferenceSite",
    "emission_kind",
    "git_identity",
    "load_coverage",
    "verify_binding",
    "write_to_kg",
    "evaluate_gate",
    "selector_event_names",
]
