"""ooptdd_loop — an agent-driven, positive-TDD requirements loop.

This is the *application* of the ``ooptdd`` library (the verify/backend/plugin
layer) as an anti-hallucination development loop:

    declare requirements as trace specs (with a Longinus source binding)
      -> an agent writes/changes code
      -> the loop RUNS the code, waits until the expected log events ARRIVE in the
         store (positive arrival, via the ooptdd backend / oo-mcp)
      -> each satisfied requirement is bound to the real emitting source (Longinus)
      -> RED requirements come back with a log-grounded RCA the agent acts on
      -> repeat until EVERY requirement is GREEN and bound.

What it actually guarantees (no magic): a requirement is GREEN only when the code,
*as run*, emitted the events into an external store the agent cannot fake, and the
Longinus binding points at source that really exists and really emits them. That
makes wrong development *detectable and self-correcting*, not impossible — the
honest version of "it can't go wrong".
"""
from .rules import canonical_rules
from .domain.spec import Contract, Methodology, Requirement, load_spec
from .runner import RunResult, evaluate_requirements, run_loop
from .tools import call, list_tools
from .golden import diff_golden, save_golden
from .local_capture import capture_logging_to_backend, structlog_event_processor

__all__ = [
    "Contract",
    "Methodology",
    "Requirement",
    "RunResult",
    "call",
    "canonical_rules",
    "capture_logging_to_backend",
    "diff_golden",
    "evaluate_requirements",
    "list_tools",
    "load_spec",
    "run_loop",
    "save_golden",
    "structlog_event_processor",
]
__version__ = "0.1.0"
