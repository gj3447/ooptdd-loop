"""V2 judge — once a run is persisted, coverage and drift are answerable by query.

Pre-registered metric (LakatosTree_ooptdd_ontology_20260616 / V2-kg-native-loop-io):
  (a) coverage(spec) returns done/total matching the run, AND
  (b) drift(spec) flags a requirement whose source sha256 changed between runs,
  BOTH by query alone (no re-run) — and the offline invariant holds (the loop
  runs fine with no KG store at all).
"""
import os
from types import SimpleNamespace

from ooptdd.backends import memory_reset
from ooptdd_loop.kg import InMemoryKgStore
from ooptdd_loop.runner import run_loop
from ooptdd_loop.domain.spec import load_spec

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
MET = os.path.join(ROOT, "example", "requirements.yaml")
UNMET = os.path.join(ROOT, "example", "requirements_unmet.yaml")


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _site(anchor, sha):
    return SimpleNamespace(kg_anchor=anchor, sha256=sha, source_path="shop.py", symbol="f")


def _res(rid, done, binding):
    return SimpleNamespace(id=rid, gate_ok=done, reachable=True, bound=True,
                           done=done, binding=binding)


# ── (a) coverage from a real loop run, queried from the store ─────────────────
def test_coverage_from_loop_run():
    store = InMemoryKgStore()
    run = run_loop(load_spec(MET), kg_store=store)
    assert run.complete
    cov = store.coverage("requirements")          # spec name = file stem
    assert cov["total"] == 4 and cov["done"] == 4 and cov["complete"] is True
    assert cov["incomplete"] == []


def test_coverage_reports_incomplete():
    store = InMemoryKgStore()
    run_loop(load_spec(UNMET), kg_store=store)
    cov = store.coverage("requirements_unmet")
    assert cov["complete"] is False
    assert "REQ-FRAUD" in cov["incomplete"]


# ── (b) drift detected purely from the store, across two runs ─────────────────
def test_drift_flags_changed_sha():
    store = InMemoryKgStore()
    store.write_run("c1", "s", [_res("R1", True, _site("anchor-a", "AAAA"))])
    assert store.drift("s") == []                  # baseline only -> no drift
    store.write_run("c2", "s", [_res("R1", True, _site("anchor-a", "BBBB"))])
    d = store.drift("s")
    assert len(d) == 1
    assert d[0]["kg_anchor"] == "anchor-a"
    assert d[0]["baseline"] == "AAAA" and d[0]["current"] == "BBBB"


def test_no_drift_when_sha_stable():
    store = InMemoryKgStore()
    store.write_run("c1", "s", [_res("R1", True, _site("a", "AAAA"))])
    store.write_run("c2", "s", [_res("R1", True, _site("a", "AAAA"))])
    assert store.drift("s") == []


# ── offline invariant: the loop runs with NO kg store, unchanged ──────────────
def test_loop_runs_without_kg_store():
    run = run_loop(load_spec(MET))                 # kg_store=None
    assert run.complete and run.n_done == 4
