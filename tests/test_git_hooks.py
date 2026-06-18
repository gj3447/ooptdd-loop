"""git_hooks — git activity shipped to ooptdd as structured logs (not email)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

# Load git_hooks as a standalone module (by file path), mirroring how the installed hook runs
# it — so the test never pulls in the heavy ooptdd_loop package __init__.
_SPEC = importlib.util.spec_from_file_location(
    "git_hooks_under_test",
    pathlib.Path(__file__).resolve().parents[1] / "ooptdd_loop" / "git_hooks.py",
)
git_hooks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(git_hooks)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _repo(path, remote="git@example.com:acme/widget.git"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "remote", "add", "origin", remote)
    return path


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # confine capture + global git config + hooks dir to tmp (never touch real ~)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OOPTDD_GIT_LOG", raising=False)
    monkeypatch.delenv("OOPTDD_GIT_HOOKS_DIR", raising=False)
    monkeypatch.delenv("OOPTDD_OO_URL", raising=False)
    (tmp_path / "home").mkdir()


def test_emit_appends_local_jsonl(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.chdir(repo)

    ev = git_hooks.emit("commit")
    assert ev["event"] == "git.commit"
    assert ev["repo_id"] == "example.com/acme/widget"
    assert ev["cid"] == ev["correlation_id"] == ev["cycle_id"] == "example.com/acme/widget"
    assert ev["service"] == "git" and ev["level"] == "INFO"
    assert ev["subject"] == "init" and ev["commit"]

    # durable local capture has exactly one matching line
    lines = git_hooks.capture_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "git.commit" and rec["repo_id"] == "example.com/acme/widget"


def test_emit_never_raises_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path / "home")  # not a git repo
    ev = git_hooks.emit("commit")  # must not raise
    assert ev is not None and ev["repo_id"] == "unknown"


def test_emit_no_remote_ship_without_oo_url(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    monkeypatch.chdir(repo)
    # OOPTDD_OO_URL unset (fixture) -> remote ship skipped, local capture still happens
    git_hooks.emit("checkout")
    assert git_hooks.capture_path().exists()


def test_install_writes_hooks_and_sets_hookspath(tmp_path):
    d = git_hooks.install(python="/usr/bin/python3")
    for hook in ("post-commit", "post-merge", "post-checkout", "post-rewrite", "pre-push"):
        f = d / hook
        assert f.exists() and f.stat().st_mode & 0o111  # executable
        body = f.read_text()
        assert "git_hooks.py" in body and "emit" in body and "exit 0" in body
    # post-commit logs the commit op, via the standalone script path (not `python -m`)
    pc = (d / "post-commit").read_text()
    assert "emit commit" in pc and "-m ooptdd_loop" not in pc
    # global core.hooksPath now points at our dir (HOME is tmp, so this is isolated)
    cur = subprocess.run(["git", "config", "--global", "core.hooksPath"],
                         capture_output=True, text=True).stdout.strip()
    assert cur == str(d)


def test_status_and_uninstall_roundtrip(tmp_path):
    git_hooks.install()
    assert git_hooks.status()["installed"] is True
    git_hooks.uninstall()
    assert git_hooks.status()["installed"] is False


def test_normalize_remote_forms():
    n = git_hooks._normalize_remote
    assert n("https://github.com/o/r.git") == "github.com/o/r"
    assert n("git@github.com:o/r.git") == "github.com/o/r"
    assert n(None) is None
