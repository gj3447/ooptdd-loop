"""L6 charge-coverage — executed-but-unobserved emit sites (the un-emitted-path gap, partially closed).

The pure logic (AST emit-site detection, observed-type extraction, gap classification) is tested
WITHOUT coverage.py via a fake controller; a real-measurement integration test is skipped unless
coverage.py is installed. Everything degrades to a no-op without crashing when the feature is off.
"""
import textwrap

import pytest

from ooptdd_loop.charge_coverage import (
    _NullController,
    build_charge_report,
    charge_enabled,
    coverage_session,
    emit_sites,
    observed_types,
)
from ooptdd_loop.report import render
from ooptdd_loop.runner import RunResult

SAMPLE = textwrap.dedent('''
    def run(backend, cid, log):
        backend.write({"event": "charge.ok"})       # 2  store dict -> charge.ok
        backend.write({"event": "charge.skip"})     # 3  store dict -> charge.skip (pretend unrun)
        log.info("just a human message")            # 4  log msg -> opaque (no static event)
        log.info("shipped", extra={"event": "log.ship"})  # 5  log extra -> log.ship
        emit("ship.done")                           # 6  store positional -> ship.done
        helper(compute())                           # 7  not an emit
''').lstrip("\n")


class _FakeCtl:
    """Stands in for a real coverage controller: hand it the lines we pretend executed."""

    enabled = True
    note = ""

    def __init__(self, file, executed):
        self.measured_files = [file]
        self._executed = set(executed)

    def executed_lines(self, file):
        return self._executed


def _write(tmp_path, text):
    p = tmp_path / "sut.py"
    p.write_text(text)
    return str(p)


# --------------------------------------------------------------------------- AST detection

def test_emit_sites_extracts_names_and_distinguishes_store_vs_log(tmp_path):
    path = _write(tmp_path, SAMPLE)
    sites = dict(emit_sites(path))  # {lineno: event_or_None}
    assert sites[2] == "charge.ok"          # store dict event=
    assert sites[3] == "charge.skip"
    assert sites[4] is None                 # a log MESSAGE is not an event id -> opaque
    assert sites[5] == "log.ship"           # log extra={"event": ...} is
    assert sites[6] == "ship.done"          # store positional string
    assert 7 not in sites                   # helper()/compute() are not emit-looking


def test_emit_sites_reads_dicts_inside_a_list_arg(tmp_path):
    # the codebase idiom: backend.ship([{...}, {...}]) — one call, two events, on one line.
    path = _write(tmp_path, textwrap.dedent('''
        def run(backend, cid):
            backend.ship([{"event": "a.one", "cid": cid}, {"event": "a.two"}])
            backend.ship([wrap(cid, "hidden")])   # wrapped -> not statically nameable
    ''').lstrip("\n"))
    sites = emit_sites(path)
    assert (2, "a.one") in sites and (2, "a.two") in sites   # both events on line 2
    assert (3, None) in sites                                # wrapper hides the name -> opaque


def test_emit_sites_on_garbage_is_empty(tmp_path):
    bad = _write(tmp_path, "def f(:\n  pass\n")  # syntax error
    assert emit_sites(bad) == []
    assert emit_sites(str(tmp_path / "nope.py")) == []  # missing file


def test_observed_types_pulls_every_name_key():
    events = [
        {"event": "charge.ok"}, {"event_type": "x.y"}, {"name": "n"}, {"type": "t"},
        {"nope": "ignored"}, "not-a-dict", {"event": 7},  # non-str ignored
    ]
    assert observed_types(events) == {"charge.ok", "x.y", "n", "t"}


# --------------------------------------------------------------------------- gap classification

def test_build_report_classifies_gaps_corroborated_and_opaque(tmp_path):
    path = _write(tmp_path, SAMPLE)
    # executed everything EXCEPT line 3 (charge.skip); store saw only charge.ok + log.ship.
    ctl = _FakeCtl(path, executed={2, 4, 5, 6})
    events = [{"event": "charge.ok"}, {"event": "log.ship"}]
    rep = build_charge_report(ctl, events)

    assert rep.enabled is True
    gap_events = {s.event for s in rep.gaps}
    ok_events = {s.event for s in rep.corroborated}
    opaque_lines = {s.line for s in rep.opaque}

    assert gap_events == {"ship.done"}      # executed (line 6), name never arrived -> GAP
    assert ok_events == {"charge.ok", "log.ship"}   # executed and arrived
    assert opaque_lines == {4}              # executed log message, no static name
    # charge.skip (line 3) was NOT executed -> neither a gap nor corroborated
    assert "charge.skip" not in gap_events and "charge.skip" not in ok_events


def test_summary_lists_gaps_and_states_the_limit(tmp_path):
    path = _write(tmp_path, SAMPLE)
    rep = build_charge_report(_FakeCtl(path, executed={2, 6}),
                              [{"event": "charge.ok"}])
    text = rep.summary()
    assert "ship.done" in text and "executed-but-UNOBSERVED" in text
    assert "EXECUTED paths only" in text   # the honest ceiling is printed, not hidden


# --------------------------------------------------------------------------- disabled / no-op safety

def test_disabled_report_is_inert():
    rep = build_charge_report(_NullController(note="off"), [{"event": "x"}])
    assert rep.enabled is False and rep.gaps == [] and "off" in rep.summary()
    assert build_charge_report(None, []).enabled is False


def test_coverage_session_off_by_default_yields_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("OOPTDD_CHARGE_COVERAGE", raising=False)
    assert charge_enabled() is False
    with coverage_session([_write(tmp_path, SAMPLE)]) as ctl:
        pass
    assert ctl.enabled is False  # the SUT body still ran; nothing measured


def test_coverage_session_enabled_without_coverage_pkg_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("OOPTDD_CHARGE_COVERAGE", "1")
    try:
        import coverage  # noqa: F401
        pytest.skip("coverage.py installed — exercised by the integration test")
    except ImportError:
        pass
    with coverage_session([_write(tmp_path, SAMPLE)]) as ctl:
        pass
    assert ctl.enabled is False and "coverage.py not installed" in ctl.note


def test_render_shows_advisory_only_when_enabled(tmp_path):
    run = RunResult(cid="c", backend="memory")
    assert "charge-coverage" not in render(run)            # None -> silent
    run.charge = build_charge_report(_NullController(), [])
    assert "charge-coverage" not in render(run)            # disabled -> silent
    run.charge = build_charge_report(_FakeCtl(_write(tmp_path, SAMPLE), {2}),
                                     [{"event": "charge.ok"}])
    assert "charge-coverage" in render(run)                # enabled -> printed


# --------------------------------------------------------------------------- real measurement

def test_real_coverage_measures_executed_emit_lines(tmp_path, monkeypatch):
    pytest.importorskip("coverage")
    monkeypatch.setenv("OOPTDD_CHARGE_COVERAGE", "1")
    src = textwrap.dedent('''
        def emit(x):
            pass

        def run(taken):
            emit("a.ran")
            if taken:
                emit("b.taken")
            else:
                emit("c.untaken")
    ''').lstrip("\n")
    path = _write(tmp_path, src)
    import importlib.util
    spec = importlib.util.spec_from_file_location("charge_sut", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with coverage_session([path]) as ctl:
        mod.run(taken=True)   # executes a.ran + b.taken, never c.untaken
    assert ctl.enabled is True

    rep = build_charge_report(ctl, [{"event": "a.ran"}])  # store saw only a.ran
    gap_events = {s.event for s in rep.gaps}
    ok_events = {s.event for s in rep.corroborated}
    assert ok_events == {"a.ran"}          # executed AND observed
    assert "b.taken" in gap_events         # executed but never reached the store
    assert "c.untaken" not in gap_events   # the un-executed branch is honestly out of scope
