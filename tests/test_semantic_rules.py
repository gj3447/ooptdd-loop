"""Methodology rules R04/R06 are answered from the *source code*, not just spec shape.

``emission_kind`` reads the bound symbol's AST and tells a structured event emission
(``{"event": "..."}`` / an event token) from a free-text log line; R04 fails a binding
whose code only logs the event as prose, and R06 fails a binding to a private helper.
"""
from __future__ import annotations

from ooptdd_loop.engine.longinus import emission_kind
from ooptdd_loop.rules import evaluate_spec_rules
from ooptdd_loop.domain.spec import Longinus, Methodology, Requirement, Spec, Target


def _spec(tmp_path, body: str, *, symbol="emit", must_emit="paid"):
    (tmp_path / "svc.py").write_text(body, encoding="utf-8")
    return Spec(
        target=Target(mode="in_process", callable="svc:emit", backend="memory",
                      root=str(tmp_path)),
        requirements=[Requirement(
            id="REQ-1", description="payment", kind="guiding",
            gate=[{"event": must_emit, "op": "==", "count": 1}],
            longinus=Longinus(kg_anchor="ref:1", source="svc.py", symbol=symbol,
                              must_emit=must_emit),
        )],
        methodology=Methodology(name="OOPTDD_methodology_v1", enforce=True),
    )


def _check(checks, rule_id):
    return next(c for c in checks if c.rule_id == rule_id)


# ── emission_kind classification ───────────────────────────────────────────────
def test_structured_when_event_is_a_token(tmp_path):
    spec = _spec(tmp_path, 'def emit():\n    ship({"event": "paid"})\n')
    assert emission_kind(spec.target.root, spec.requirements[0].longinus) == "structured"


def test_structured_when_literal_passed_as_event_token(tmp_path):
    spec = _spec(tmp_path, 'def emit():\n    record("paid", amount=1)\n')
    assert emission_kind(spec.target.root, spec.requirements[0].longinus) == "structured"


def test_free_text_when_buried_in_a_log_sentence(tmp_path):
    spec = _spec(tmp_path, 'def emit():\n    logger.info("the order was paid for now")\n',
                 must_emit="paid")
    assert emission_kind(spec.target.root, spec.requirements[0].longinus) == "free_text"


# ── R04 is answered from code ──────────────────────────────────────────────────
def test_r04_passes_for_structured_emission(tmp_path):
    spec = _spec(tmp_path, 'def emit():\n    ship({"event": "paid"})\n')
    assert _check(evaluate_spec_rules(spec, root=spec.target.root), "OOPTDD-R04").passed


def test_r04_fails_for_free_text_emission(tmp_path):
    spec = _spec(tmp_path, 'def emit():\n    logger.info("the order was paid for now")\n')
    c = _check(evaluate_spec_rules(spec, root=spec.target.root), "OOPTDD-R04")
    assert not c.passed and "free-text emission in source" in c.message


def test_r04_without_root_falls_back_to_spec_shape(tmp_path):
    # no root → no source analysis → spec-shape gates still structured → pass (legacy)
    spec = _spec(tmp_path, 'def emit():\n    logger.info("the order was paid for now")\n')
    assert _check(evaluate_spec_rules(spec, root=None), "OOPTDD-R04").passed


# ── R06 rejects a private bound symbol ─────────────────────────────────────────
def test_r06_fails_for_private_bound_symbol(tmp_path):
    spec = _spec(tmp_path, 'def _emit():\n    ship({"event": "paid"})\n', symbol="_emit")
    c = _check(evaluate_spec_rules(spec, root=spec.target.root), "OOPTDD-R06")
    assert not c.passed and "private bound symbols" in c.message
