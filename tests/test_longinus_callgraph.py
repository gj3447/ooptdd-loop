"""Longinus binds through the call graph, and (optionally) requires runtime reachability.

The original check only looked at the entry symbol's own body, so a requirement bound to
an entry point that *delegates* emission to a helper read as UNBOUND — a false RED. These
pin the transitive binding (with a recorded ``via`` chain), the cross-module hop, the
loop-guard, and the coverage-gated runtime check.
"""
from __future__ import annotations

from ooptdd_loop.engine.longinus import verify_binding
from ooptdd_loop.domain.spec import Longinus


def _lon(symbol="entry", must_emit="cycle.done", source="mod.py"):
    return Longinus(kg_anchor="ref:test", source=source, symbol=symbol, must_emit=must_emit)


def test_bound_through_a_called_helper(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def _ship():\n"
        '    log({"event": "cycle.done"})\n'
        "def entry():\n"
        "    _ship()\n",
        encoding="utf-8",
    )
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is True
    assert site.via == ("_ship",)            # entry delegated to _ship
    assert site.reason == "bound via entry->_ship"
    assert site.emit_line == 2


def test_bound_through_a_two_hop_chain(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def emit():\n"
        '    publish("cycle.done")\n'
        "def mid():\n"
        "    emit()\n"
        "def entry():\n"
        "    mid()\n",
        encoding="utf-8",
    )
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is True and site.via == ("mid", "emit")


def test_bound_across_modules_under_root(tmp_path):
    (tmp_path / "emitter.py").write_text(
        'def do_emit():\n    send({"event": "cycle.done"})\n', encoding="utf-8")
    (tmp_path / "mod.py").write_text(
        "from emitter import do_emit\n"
        "def entry():\n"
        "    do_emit()\n",
        encoding="utf-8",
    )
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is True and site.via == ("do_emit",)
    assert site.emit_path.endswith("emitter.py")


def test_still_unbound_when_no_reachable_emitter(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def helper():\n    return 1\n"
        "def entry():\n    helper()\n",
        encoding="utf-8",
    )
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is False and "reachable callees" in site.reason


def test_mutual_recursion_does_not_hang(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def a():\n    b()\n"
        "def b():\n    a()\n"
        "def entry():\n    a()\n",
        encoding="utf-8",
    )
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is False  # terminates via the visited-set guard, no emit anywhere


def test_direct_emit_has_empty_via_and_plain_bound_reason(tmp_path):
    (tmp_path / "mod.py").write_text(
        'def entry():\n    log({"event": "cycle.done"})\n', encoding="utf-8")
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is True and site.via == () and site.reason == "bound"


# ── runtime reachability via a coverage line-map ───────────────────────────────
def test_runtime_reached_true_when_emit_line_executed(tmp_path):
    (tmp_path / "mod.py").write_text(
        'def entry():\n    log({"event": "cycle.done"})\n', encoding="utf-8")
    emit_file = str(tmp_path / "mod.py")
    site = verify_binding(str(tmp_path), _lon(), cov_lines={emit_file: {2}})
    assert site.bound is True and site.runtime_reached is True


def test_require_runtime_fails_when_line_never_executed(tmp_path):
    (tmp_path / "mod.py").write_text(
        'def entry():\n    log({"event": "cycle.done"})\n', encoding="utf-8")
    emit_file = str(tmp_path / "mod.py")
    site = verify_binding(str(tmp_path), _lon(),
                          cov_lines={emit_file: {1}},  # line 1 ran, line 2 (the emit) did not
                          require_runtime=True)
    assert site.bound is False and site.runtime_reached is False
    assert "runtime-unreachable" in site.reason


def test_no_coverage_map_leaves_runtime_unknown(tmp_path):
    (tmp_path / "mod.py").write_text(
        'def entry():\n    log({"event": "cycle.done"})\n', encoding="utf-8")
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is True and site.runtime_reached is None
