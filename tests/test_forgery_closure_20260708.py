"""RED-first closure of three loop forgery paths (audit 2026-07-08).

Each test proves a forgery *succeeds* against the unfixed engine (so it is RED
before the fix) and is caught after it, and each is revert-proof: undo the
production edit and the matching assertion goes RED again.

- #6  selector_gates.evaluate_gate: an all-optional / all-pending gate had zero
      GATING checks, so `bool(checks) and all([])` was a free GREEN. Fixed by
      guarding on `bool(gating)`.
- #7  evaluate_gate dropped the upstream enforcement verdict: a
      require_signature / require_corroboration failure surfaces on the upstream
      result as top-level `unauthenticated` / `uncorroborated` (NOT as a check),
      so merging only `out["checks"]` silently disabled it. Fixed by vetoing on
      exactly those two flags (NOT `out["ok"]`, which is vacuously False for a
      selector-only delegation and would false-RED honest specs).
- #8  longinus._emit_line counted a string constant that is the whole value of an
      expression statement (a docstring or a dead bare-string), so "emit it
      elsewhere, name it in the claimed symbol's docstring" bound a DONE. Fixed by
      excluding Expr-value string constants (zero runtime effect, never emission).

Scope note for #8: this closes the docstring / dead-string vector precisely. The
same *class* of static-but-unreachable forgery (a `if False:` dead branch, an
uncalled nested def) is NOT closed here — that needs the runtime-reachability gate
(require_runtime + a real coverage map). Tracked as a follow-up.
"""
from __future__ import annotations

import pytest

from ooptdd.backends import MemoryBackend, memory_reset

from ooptdd_loop.engine.selector_gates import evaluate_gate
from ooptdd_loop.engine.longinus import verify_binding
from ooptdd_loop.domain.spec import Longinus


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # No ambient signing key: the enforcement tests want an unsigned stream.
    monkeypatch.delenv("OOPTDD_SIGNING_KEY", raising=False)
    memory_reset()
    yield
    memory_reset()


# --------------------------------------------------------------------------- #6
def test_all_optional_gate_is_not_green():
    """#6: every rule optional -> zero gating checks -> must NOT be a clean pass."""
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "_timestamp": 1}])
    res = evaluate_gate(b, {"cid": "c", "expect": [
        {"event": "paid", "op": ">=", "count": 1, "optional": True},
    ]})
    assert res["checks"] and all(c["optional"] for c in res["checks"])  # nothing gates
    assert res["ok"] is False  # was True via bool(checks) + all([])


def test_all_pending_gate_is_not_green():
    """#6: pending is excluded from gating exactly like optional."""
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "_timestamp": 1}])
    res = evaluate_gate(b, {"cid": "c", "expect": [
        {"event": "paid", "op": ">=", "count": 1, "pending": True},
    ]})
    assert res["ok"] is False
    assert res["pending_satisfied"]  # verified & surfaced, just not gating


def test_empty_gate_stays_not_green():
    """#6 guard: the pre-existing empty-gate veto must survive the bool(gating) change."""
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "_timestamp": 1}])
    res = evaluate_gate(b, {"cid": "c", "expect": []})
    assert res["ok"] is False and res["empty"] is True


# --------------------------------------------------------------------------- #7
def test_require_signature_failure_vetoes_loop_ok(monkeypatch):
    """#7: a require_signature failure vetoes ok even when the positive check passes."""
    monkeypatch.setenv("OOPTDD_REQUIRE_SIGNATURE", "1")
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "_timestamp": 1}])  # unsigned
    res = evaluate_gate(b, {"cid": "c", "expect": [
        {"event": "paid", "op": ">=", "count": 1},  # non-selector -> upstream asserts
    ]})
    assert res["checks"][0]["passed"] is True   # the expectation IS satisfied
    assert res["ok"] is False                    # ...but signature enforcement vetoes


def test_require_corroboration_failure_vetoes_loop_ok(monkeypatch):
    """#7: a require_corroboration failure (single self-authority) vetoes ok."""
    monkeypatch.setenv("OOPTDD_REQUIRE_CORROBORATION", "1")
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "_timestamp": 1}])
    res = evaluate_gate(b, {"cid": "c", "expect": [
        {"event": "paid", "op": ">=", "count": 1},  # self-report only
    ]})
    assert res["checks"][0]["passed"] is True
    assert res["ok"] is False


def test_spec_key_require_signature_reaches_upstream_and_vetoes():
    """#7 companion: a spec-authored `require_signature: true` must reach upstream.

    Without the passthrough forward it is dropped before enforcement and the unsigned
    stream is a free GREEN.
    """
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "_timestamp": 1}])  # unsigned
    res = evaluate_gate(b, {"cid": "c", "require_signature": True, "expect": [
        {"event": "paid", "op": ">=", "count": 1},
    ]})
    assert res["checks"][0]["passed"] is True
    assert res["ok"] is False


def test_honest_selector_only_ok_tracks_check_passed():
    """#7 over-reach guard: the veto must NOT touch an honest selector-only spec.

    A naive `and out["ok"]` fix would false-RED this (upstream ok is vacuously False
    for an empty delegation).
    """
    b = MemoryBackend()
    b.ship([{"cid": "c", "event": "paid", "service": "billing", "_timestamp": 1}])
    res = evaluate_gate(b, {"cid": "c", "expect": [
        {"select": {"event": "paid", "service": "billing"}, "op": ">=", "count": 1},
    ]})
    assert res["checks"][0]["passed"] is True and res["ok"] is True


# --------------------------------------------------------------------------- #8
def _write(tmp_path, body: str) -> str:
    (tmp_path / "mod.py").write_text(body, encoding="utf-8")
    return str(tmp_path)


def _lon(symbol="emit_it", must_emit="cycle.done"):
    return Longinus(kg_anchor="ref:test", source="mod.py", symbol=symbol, must_emit=must_emit)


def test_unbound_when_literal_only_in_a_docstring(tmp_path):
    """#8: a docstring naming the event is not emission."""
    root = _write(
        tmp_path,
        'def emit_it():\n'
        '    """This will emit cycle.done to the bus."""\n'
        '    return 1\n',
    )
    site = verify_binding(root, _lon())
    assert site.bound is False


def test_unbound_when_literal_only_in_a_dead_string_statement(tmp_path):
    """#8: a bare string statement is a runtime no-op and cannot emit."""
    root = _write(
        tmp_path,
        'def emit_it():\n'
        '    "cycle.done"\n'
        '    return 1\n',
    )
    site = verify_binding(root, _lon())
    assert site.bound is False


def test_docstring_does_not_bind_when_real_emit_is_unreachable(tmp_path):
    """#8 full scenario: emit from an unrelated symbol, name it in the claimed docstring."""
    root = _write(
        tmp_path,
        'def elsewhere():\n'
        '    ship({"event": "cycle.done"})\n'
        'def emit_it():\n'
        '    """emit_it emits cycle.done (it does not)."""\n'
        '    return helper()\n'
        'def helper():\n'
        '    return 1\n',
    )
    site = verify_binding(root, _lon())
    assert site.bound is False


def test_bound_when_docstring_names_it_but_body_really_emits(tmp_path):
    """#8 anti-over-reach: a docstring next to a real emit still binds, at the REAL line."""
    root = _write(
        tmp_path,
        'def emit_it():\n'
        '    """emits cycle.done"""\n'
        '    ship({"event": "cycle.done"})\n',
    )
    site = verify_binding(root, _lon())
    assert site.bound is True
    assert site.emit_line == 3  # the real emit, not the docstring on line 2
