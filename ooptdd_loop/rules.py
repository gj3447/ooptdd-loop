# KG: OOPTDD_methodology_v1
"""OOPTDD methodology rules and spec-level rule checks.

The loop already verifies dynamic trace arrival and Longinus source binding.
This module adds the static half: the object/role/message rules that keep
OOPTDD from degrading into class-by-class unit tests or log-only assertions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .engine.longinus import emission_kind


@dataclass(frozen=True)
class MethodologyRule:
    id: str
    name: str
    title: str
    category: str
    statement: str
    severity: str = "error"
    depends_on: tuple[str, ...] = ()


@dataclass
class RuleCheck:
    rule_id: str
    passed: bool
    severity: str
    message: str


OOPTDD_METHOD_NAME = "OOPTDD_methodology_v1"
OOPTDD_METHOD_TITLE = "OOPTDD agentic object-positive TDD"


def canonical_rules() -> list[MethodologyRule]:
    """Canonical OOPTDD rules, ordered as the engine evaluates them."""
    return [
        MethodologyRule(
            "OOPTDD-R01",
            "ooptdd-rule-guiding-test-first",
            "Guiding test first",
            "outside_in",
            "Every enforced OOPTDD spec starts from a feature/guiding requirement.",
        ),
        MethodologyRule(
            "OOPTDD-R02",
            "ooptdd-rule-positive-trace-arrival",
            "Positive trace arrival",
            "ltdd",
            "A requirement is not green until expected structured events arrive.",
        ),
        MethodologyRule(
            "OOPTDD-R03",
            "ooptdd-rule-correlation-id",
            "Correlation id boundary",
            "ltdd",
            "All runtime evidence is scoped by a per-run correlation id.",
            depends_on=("OOPTDD-R02",),
        ),
        MethodologyRule(
            "OOPTDD-R04",
            "ooptdd-rule-structured-events",
            "Structured events only",
            "ltdd",
            "Assertions target event structure, not free-text log messages.",
            depends_on=("OOPTDD-R02",),
        ),
        MethodologyRule(
            "OOPTDD-R05",
            "ooptdd-rule-mock-as-contract-candidate",
            "Mock as contract candidate",
            "ltdd_absorption",
            "A mock expectation may become a MessageContract only when it names a role "
            "protocol, not an incidental dependency.",
        ),
        MethodologyRule(
            "OOPTDD-R06",
            "ooptdd-rule-domain-message-only",
            "Domain messages only",
            "object_design",
            "Private helpers, getters, and algorithm internals are not KG contracts.",
            depends_on=("OOPTDD-R05",),
        ),
        MethodologyRule(
            "OOPTDD-R07",
            "ooptdd-rule-role-responsibility-protocol",
            "Role responsibility protocol",
            "object_design",
            "Objects are defined by role, responsibility, and protocol before class shape.",
        ),
        MethodologyRule(
            "OOPTDD-R08",
            "ooptdd-rule-interaction-state-split",
            "Interaction/state split",
            "object_design",
            "Message contracts and state invariants are separate KG nodes.",
        ),
        MethodologyRule(
            "OOPTDD-R09",
            "ooptdd-rule-integration-backstop",
            "Integration backstop",
            "testing",
            "An accepted MessageContract needs a real integration/guiding backstop.",
            depends_on=("OOPTDD-R05",),
        ),
        MethodologyRule(
            "OOPTDD-R10",
            "ooptdd-rule-longinus-reference-site",
            "Longinus ReferenceSite",
            "longinus",
            "Every materialized requirement has a resolvable Longinus binding.",
        ),
        MethodologyRule(
            "OOPTDD-R11",
            "ooptdd-rule-reverse-orphan-scan",
            "Reverse orphan scan",
            "longinus",
            "KG refs in code must resolve back to KG nodes; orphan refs are drift.",
            depends_on=("OOPTDD-R10",),
        ),
        MethodologyRule(
            "OOPTDD-R12",
            "ooptdd-rule-log-free-zone",
            "Log-free zone",
            "safety",
            "Precision numeric, security, and micro-race checks must declare a "
            "non-log oracle.",
        ),
        MethodologyRule(
            "OOPTDD-R13",
            "ooptdd-rule-agent-work-unit",
            "Agent work unit",
            "agent",
            "Agent tasks carry red spec, expected green evidence, source binding, and "
            "next-step RCA.",
        ),
        MethodologyRule(
            "OOPTDD-R14",
            "ooptdd-rule-done-means-green-bound-valid",
            "Done means green, bound, valid",
            "agent",
            "A requirement is DONE only when the trace gate is green, Longinus is bound, "
            "and enforced OOPTDD rules pass.",
            depends_on=("OOPTDD-R02", "OOPTDD-R10"),
        ),
    ]


def canonical_rule_map() -> dict[str, MethodologyRule]:
    return {r.id: r for r in canonical_rules()}


def rules_as_dicts() -> list[dict]:
    return [asdict(r) for r in canonical_rules()]


def is_ooptdd_enabled(spec) -> bool:
    name = (spec.methodology.name or "").lower()
    return spec.methodology.enforce or name.startswith("ooptdd")


def evaluate_spec_rules(spec, root: str | None = None) -> list[RuleCheck]:
    """Evaluate OOPTDD methodology rules against a loaded spec.

    Specs that do not opt in via ``methodology.name`` or ``methodology.enforce``
    keep legacy behavior and return no checks.

    When ``root`` is given (the source root), the rules that *can* be answered from code
    are answered from code — R04 inspects whether each bound symbol emits a **structured**
    event (vs a free-text log line) and R06 checks the bound symbol is not a private helper
    in source. Without ``root`` those rules fall back to their spec-shape form, so callers
    that pass only a spec keep the previous behavior.
    """
    if not is_ooptdd_enabled(spec):
        return []

    req_ids = {r.id for r in spec.requirements}
    contract_ids = {c.id for c in spec.contracts}
    covered = {cid for r in spec.requirements for cid in r.covers}
    checks: list[RuleCheck] = []

    def add(rule_id: str, passed: bool, message: str) -> None:
        rule = canonical_rule_map()[rule_id]
        checks.append(RuleCheck(rule_id, passed, rule.severity, message))

    guiding = [
        r for r in spec.requirements
        if r.kind in {"guiding", "acceptance", "feature"}
    ]
    add(
        "OOPTDD-R01",
        bool(guiding),
        "at least one guiding/acceptance/feature requirement is required",
    )

    gateful = [
        r for r in spec.requirements
        if r.gate and all(_known_gate_shape(c) for c in r.gate)
    ]
    add(
        "OOPTDD-R02",
        len(gateful) == len(spec.requirements),
        "every requirement must declare event/conforms/must_order trace gates",
    )
    add(
        "OOPTDD-R03",
        spec.target.mode in {"in_process", "command", "pytest"},
        "engine mints and propagates OOPTDD_CID for in_process/command/pytest targets",
    )
    # R04 spec-shape half: the declared gates must be structured (event/conforms/...).
    # Code half (when root is known): the bound symbol must emit a *structured* event,
    # not bury the event name in a free-text log line — analysed from the source AST.
    free_text_reqs = []
    if root:
        for r in spec.requirements:
            if r.longinus is not None and emission_kind(root, r.longinus) == "free_text":
                free_text_reqs.append(r.id)
    add(
        "OOPTDD-R04",
        _all_gates_structured(spec.requirements) and not free_text_reqs,
        "free-text log assertions are not accepted; use event/conforms/must_order/select"
        + (f" (free-text emission in source: {free_text_reqs})" if free_text_reqs else ""),
    )

    backed_contracts = [
        c for c in spec.contracts
        if not c.source_req or c.source_req in req_ids
    ]
    add(
        "OOPTDD-R05",
        len(backed_contracts) == len(spec.contracts),
        "each contract candidate must point to an existing source_req",
    )

    bad_private = [
        c.id for c in spec.contracts
        if c.kind == "message_contract"
        and (not c.is_domain_message or c.message.startswith("_"))
    ]
    # Code half: a requirement bound to a private helper symbol (``_foo``) is emitting from
    # an implementation internal, not a domain message — forbid it on the source side too.
    bad_private_syms = [
        r.id for r in spec.requirements
        if r.longinus is not None and r.longinus.symbol.startswith("_")
    ]
    add(
        "OOPTDD-R06",
        not bad_private and not bad_private_syms,
        f"private/non-domain message contracts are forbidden: {bad_private}"
        + (f"; private bound symbols: {bad_private_syms}" if bad_private_syms else ""),
    )

    roles_ok = all(
        c.kind != "message_contract" or c.role or c.sender or c.receiver
        for c in spec.contracts
    )
    add(
        "OOPTDD-R07",
        roles_ok,
        "message contracts must name at least one role/sender/receiver",
    )

    mixed = [
        c.id for c in spec.contracts
        if c.kind == "message_contract" and c.extras.get("invariant")
    ]
    add(
        "OOPTDD-R08",
        not mixed,
        f"state invariant must be a separate contract node: {mixed}",
    )

    accepted_messages = [
        c for c in spec.contracts
        if c.kind == "message_contract" and c.status == "accepted"
    ]
    missing_backstop = [
        c.id for c in accepted_messages
        if not c.integration_backstop or c.integration_backstop not in req_ids
    ]
    add(
        "OOPTDD-R09",
        not missing_backstop,
        f"accepted message contracts need integration_backstop reqs: {missing_backstop}",
    )

    missing_longinus = [r.id for r in spec.requirements if r.longinus is None]
    add(
        "OOPTDD-R10",
        not missing_longinus,
        f"requirements without Longinus binding: {missing_longinus}",
    )
    add(
        "OOPTDD-R11",
        not missing_longinus,
        "reverse orphan scan is possible only when every requirement has KG anchor",
    )

    log_free_bad = [
        r.id for r in spec.requirements
        if r.kind in {"numeric_precision", "security", "micro_race"}
        and not r.extras.get("non_log_oracle")
    ]
    add(
        "OOPTDD-R12",
        not log_free_bad,
        f"log-free requirements need non_log_oracle: {log_free_bad}",
    )

    add(
        "OOPTDD-R13",
        all(r.description and r.gate for r in spec.requirements),
        "agent work units need a human-readable red spec and expected evidence",
    )
    add(
        "OOPTDD-R14",
        all(r.longinus is not None for r in spec.requirements)
        and _covers_exist(covered, contract_ids),
        "DONE can only mean green plus bound plus valid covered contract ids",
    )
    return checks


def rule_checks_ok(checks: Iterable[RuleCheck]) -> bool:
    return all(c.passed or c.severity != "error" for c in checks)


def _known_gate_shape(check: dict) -> bool:
    return any(k in check for k in ("event", "conforms", "must_order", "select", "selector"))


def _all_gates_structured(requirements) -> bool:
    for r in requirements:
        for c in r.gate:
            if "message" in c or "text" in c or not _known_gate_shape(c):
                return False
    return True


def _covers_exist(covered: set[str], contract_ids: set[str]) -> bool:
    return not (covered - contract_ids)
