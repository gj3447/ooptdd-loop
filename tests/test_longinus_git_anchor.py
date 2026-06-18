"""Longinus git-anchoring regression.

The binding is pinned to git so the same anchor resolves to the same code on any
clone, on any machine: ``verify_binding`` records the commit it was validated at, the
content-addressed ``blob_oid`` (the drift signal of record), the remote, and the
toplevel-relative ``repo_relpath``. Drift then prefers the git blob over the plain
sha256. Outside a git repo every git field is ``None`` and the sha256 fallback holds
(the offline invariant). These tests pin that contract.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from ooptdd_loop.kg import InMemoryKgStore
from ooptdd_loop.engine.longinus import ReferenceSite, git_identity, verify_binding
from ooptdd_loop.domain.spec import Longinus


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def _lon(symbol="emit_it", must_emit="cycle.done"):
    return Longinus(kg_anchor="ref:test", source="mod.py", symbol=symbol, must_emit=must_emit)


def _result(binding, *, done):
    return SimpleNamespace(id="REQ-1", gate_ok=done, reachable=True, bound=binding.bound,
                           done=done, binding=binding)


def test_binding_records_git_identity(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text('def emit_it():\n    ship({"event": "cycle.done"})\n')
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "init")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    site = verify_binding(str(repo), _lon())
    assert site.bound is True
    assert site.commit == head                 # pinned to the exact revision
    assert site.blob_oid and len(site.blob_oid) == 40   # git blob OID (sha1 hex)
    assert site.repo_relpath == "mod.py"       # toplevel-relative, not absolute


def test_blob_oid_is_content_addressed_and_machine_independent(tmp_path):
    # The same bytes hash to the same blob OID regardless of where the repo lives —
    # that is exactly what makes the anchor portable across clones/machines.
    repo = _init_repo(tmp_path)
    body = 'def emit_it():\n    ship({"event": "cycle.done"})\n'
    (repo / "mod.py").write_text(body)
    site = verify_binding(str(repo), _lon())
    expected = subprocess.run(["git", "hash-object", "--stdin"], input=body,
                              capture_output=True, text=True).stdout.strip()
    assert site.blob_oid == expected


def test_outside_git_repo_fields_are_none_but_sha256_holds(tmp_path):
    # No `git init`: not a work tree. Git fields are None; sha256 + bound still work.
    (tmp_path / "mod.py").write_text('def emit_it():\n    ship({"event": "cycle.done"})\n')
    site = verify_binding(str(tmp_path), _lon())
    assert site.bound is True
    assert len(site.sha256) == 16
    assert site.commit is None and site.blob_oid is None and site.repo_relpath is None


def test_git_identity_of_missing_file_is_all_none(tmp_path):
    ident = git_identity(str(tmp_path / "nope.py"))
    assert ident == {"toplevel": None, "commit": None, "blob_oid": None,
                     "remote": None, "repo_relpath": None}


def test_drift_uses_blob_oid_when_present(tmp_path):
    store = InMemoryKgStore()
    base = ReferenceSite("ref:a", "mod.py", "f", None, "deadbeefdeadbeef", "e", True, "bound",
                         commit="c0", blob_oid="0" * 40, repo_relpath="mod.py")
    store.write_run("cid1", "spec", [_result(base, done=True)])
    assert store.drift("spec") == []           # baseline == current → no drift

    # Same sha256, DIFFERENT blob_oid → drift detected via the git blob, not sha256.
    moved = ReferenceSite("ref:a", "mod.py", "f", None, "deadbeefdeadbeef", "e", True, "bound",
                          commit="c1", blob_oid="1" * 40, repo_relpath="mod.py")
    store.write_run("cid2", "spec", [_result(moved, done=True)])
    drift = store.drift("spec")
    assert len(drift) == 1
    assert drift[0]["blob_baseline"] == "0" * 40 and drift[0]["blob_current"] == "1" * 40
    assert drift[0]["commit"] == "c1"


def test_drift_falls_back_to_sha256_without_blob(tmp_path):
    store = InMemoryKgStore()
    base = ReferenceSite("ref:b", "mod.py", "f", None, "aaaa000000000000", "e", True, "bound")
    store.write_run("cid1", "spec", [_result(base, done=True)])
    moved = ReferenceSite("ref:b", "mod.py", "f", None, "bbbb111111111111", "e", True, "bound")
    store.write_run("cid2", "spec", [_result(moved, done=True)])
    drift = store.drift("spec")
    assert len(drift) == 1 and drift[0]["current"] == "bbbb111111111111"
