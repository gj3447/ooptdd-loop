"""Longinus binding AST-precision regression.

The original check was a plain-text substring search over the symbol's source
(``must_emit not in source_segment``), which produced false GREENs: a match in a
comment or inside an unrelated identifier counted as "the symbol emits this". The
hardened check requires the literal to appear inside a real string *constant* in the
symbol's AST subtree. These tests pin the false-positives shut while keeping every
genuine binding bound.

From the ooptdd-oss prometheus cycle (A9, seed-ooptdd-longinus-treesitter-20260618).
"""
from __future__ import annotations

from ooptdd_loop.longinus import verify_binding
from ooptdd_loop.spec import Longinus


def _write(tmp_path, body: str) -> str:
    (tmp_path / "mod.py").write_text(body, encoding="utf-8")
    return str(tmp_path)


def _lon(symbol="emit_it", must_emit="cycle.done"):
    return Longinus(kg_anchor="ref:test", source="mod.py", symbol=symbol, must_emit=must_emit)


def test_bound_when_literal_is_a_real_string_constant(tmp_path):
    root = _write(tmp_path, 'def emit_it():\n    ship({"event": "cycle.done"})\n')
    site = verify_binding(root, _lon())
    assert site.bound is True and site.reason == "bound"


def test_bound_when_literal_embedded_in_a_message_string(tmp_path):
    root = _write(tmp_path, 'def emit_it():\n    logger.info(f"[BL] finished cycle.done now")\n')
    site = verify_binding(root, _lon())
    assert site.bound is True  # containment inside a larger string still counts


def test_unbound_when_literal_only_in_a_comment(tmp_path):
    # the old substring check returned bound=True here — a false GREEN.
    root = _write(tmp_path, "def emit_it():\n    # this will emit cycle.done eventually\n    return 1\n")
    site = verify_binding(root, _lon())
    assert site.bound is False
    assert "absent from its body" in site.reason


def test_unbound_when_literal_only_as_identifier_substring(tmp_path):
    # `cycle.done` is not a string literal here; `cycle_done` is just a variable name.
    root = _write(tmp_path, "def emit_it():\n    cycle_done = True\n    return cycle_done\n")
    site = verify_binding(root, _lon(must_emit="cycle_done"))
    assert site.bound is False


def test_still_unbound_when_symbol_missing(tmp_path):
    root = _write(tmp_path, "def other():\n    pass\n")
    site = verify_binding(root, _lon())
    assert site.bound is False and "not defined" in site.reason


def test_sha256_recorded_even_when_unbound(tmp_path):
    root = _write(tmp_path, "def emit_it():\n    return 1\n")
    site = verify_binding(root, _lon())
    assert site.bound is False and len(site.sha256) == 16  # baseline still captured for drift
