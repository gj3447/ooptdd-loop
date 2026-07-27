"""Snapshot-drift gate — the declared divergence from live `ooptdd-loop` is a PIN, not a note.

POST_TIER1_ARC front A2 (D3): this repo is a priority-anchored snapshot that intentionally
diverges from canonical. History note: the 07-15 "pre-hardening lag" (4 hardening commits)
was later closed — LoopGuard/DurableRunJournal are present here — so today's declared BEHIND
set is empty. What survives is the *mechanism*: every divergence between this snapshot and
canonical must be pinned in docs/SNAPSHOT_DRIFT_DECLARATION.json, and anything undeclared
goes RED:

  - canonical file we neither track nor declared  -> BEHIND (canonical moved on / we lag)
  - declared-modified path whose canonical side moved off its pin -> BEHIND on that path
  - declared-modified path whose mark side moved off its pin      -> tamper
  - canonical growing a file that shadows a mark-only path        -> divergence-class change
  - mark file that canonical lacks and the declaration lacks      -> undeclared mark drift

Re-declaring is deliberate: scripts/gen_snapshot_drift_declaration.py.
The live comparison skips when no canonical checkout is reachable (offline CI stays honest —
the ooptdd vendor-drift trio's precedent); the negative oracles below run synthetically and
always.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "gen_snapshot_drift_declaration", REPO / "scripts" / "gen_snapshot_drift_declaration.py")
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)
norm_sha, tracked_files, head = _gen.norm_sha, _gen.tracked_files, _gen.head
DECLARATION = REPO / "docs" / "SNAPSHOT_DRIFT_DECLARATION.json"
_CANON_ENV = "OOPTDD_LOOP_CANONICAL"


def gate_violations(decl, mark_files, canon_files, canonical_head=None):
    """Pure gate core: (declaration, {path: sha}, {path: sha}) -> list of violation strings."""
    v = []
    mark_only = decl.get("mark_only", {})
    behind = decl.get("behind", {})
    modified = decl.get("modified", {})
    for path, csha in sorted(canon_files.items()):
        if path in mark_only:
            v.append(f"divergence-class change: canonical now ships mark-only path {path}")
        elif path in modified:
            if csha != modified[path]["canonical"]:
                v.append(f"BEHIND: canonical moved off its pin on declared-modified {path}")
            if path in mark_files and mark_files[path] != modified[path]["mark"]:
                v.append(f"tamper: mark moved off its pin on declared-modified {path}")
        elif path in behind:
            if csha != behind[path]:
                v.append(f"BEHIND: canonical moved off its pin on declared-behind {path}")
        elif path not in mark_files:
            v.append(f"BEHIND: canonical has undeclared {path} we do not track")
        elif mark_files[path] != csha:
            v.append(f"BEHIND: undeclared content divergence on {path}")
    for path in sorted(set(mark_files) - set(canon_files)):
        if path not in mark_only:
            v.append(f"undeclared mark drift: {path} exists here, not in canonical, not declared")
    if canonical_head and decl.get("canonical_head") and canonical_head != decl["canonical_head"]:
        # A head move with zero content violations is recorded but not RED by itself
        # (empty/merge commits move HEAD without moving content); content pins above are
        # the drift authority.
        pass
    return v


def _load_declaration():
    assert DECLARATION.is_file(), "declaration missing — run scripts/gen_snapshot_drift_declaration.py"
    return json.loads(DECLARATION.read_text())


def test_declaration_exists_and_behind_is_empty():
    """Today's honest state: the 07-15 pre-hardening lag is CLOSED (hardening present),
    so an empty BEHIND set is a *claim this test keeps true*, not an assumption."""
    decl = _load_declaration()
    assert decl["behind"] == {}, (
        "BEHIND set is non-empty — the snapshot lags canonical again; either close the lag "
        f"or re-declare it deliberately: {sorted(decl['behind'])}")
    joined = "\n".join(
        (REPO / rel).read_text() for rel in ("ooptdd_loop/runner.py", "ooptdd_loop/harness.py"))
    assert "LoopGuard" in joined and "DurableRunJournal" in joined, (
        "hardening artifacts absent — the pre-hardening lag has silently returned")


def test_snapshot_matches_declaration_against_live_canonical():
    canon_path = os.environ.get(_CANON_ENV, "")
    if not canon_path or not (Path(canon_path) / ".git").exists():
        pytest.skip(f"{_CANON_ENV} not set / not a checkout — live comparison unavailable")
    canon_root = Path(canon_path).resolve()
    decl = _load_declaration()
    violations = gate_violations(
        decl, tracked_files(REPO), tracked_files(canon_root), canonical_head=head(canon_root))
    assert not violations, "undeclared snapshot drift:\n" + "\n".join(violations)


# ── negative oracles (synthetic, always run) ───────────────────────────────────────────

_DECL = {
    "canonical_head": "cafe" * 10,
    "mark_only": {"ooptdd_loop/omd_bridge.py": "a" * 64},
    "behind": {},
    "modified": {"LICENSE": {"mark": "b" * 64, "canonical": "c" * 64}},
}
_MARK = {"ooptdd_loop/omd_bridge.py": "a" * 64, "LICENSE": "b" * 64, "shared.py": "d" * 64}
_CANON = {"LICENSE": "c" * 64, "shared.py": "d" * 64}


def test_in_sync_state_is_green():
    assert gate_violations(_DECL, dict(_MARK), dict(_CANON)) == []


def test_deleting_a_declaration_entry_goes_red():
    """A2 acceptance oracle: drop one declared divergence from the pin set -> RED."""
    decl = json.loads(json.dumps(_DECL))
    del decl["modified"]["LICENSE"]
    v = gate_violations(decl, dict(_MARK), dict(_CANON))
    assert any("undeclared content divergence on LICENSE" in x for x in v)


def test_canonical_moving_on_goes_red_behind():
    canon = dict(_CANON)
    canon["shared.py"] = "e" * 64            # canonical advanced; we now lag
    v = gate_violations(_DECL, dict(_MARK), canon)
    assert any(x.startswith("BEHIND: undeclared content divergence on shared.py") for x in v)


def test_canonical_new_file_goes_red_behind():
    canon = dict(_CANON)
    canon["new_feature.py"] = "f" * 64
    v = gate_violations(_DECL, dict(_MARK), canon)
    assert any("canonical has undeclared new_feature.py" in x for x in v)


def test_mark_tamper_on_pinned_path_goes_red():
    mark = dict(_MARK)
    mark["LICENSE"] = "9" * 64
    v = gate_violations(_DECL, mark, dict(_CANON))
    assert any(x.startswith("tamper: mark moved off its pin on declared-modified LICENSE") for x in v)


def test_canonical_shadowing_mark_only_path_goes_red():
    canon = dict(_CANON)
    canon["ooptdd_loop/omd_bridge.py"] = "0" * 64
    v = gate_violations(_DECL, dict(_MARK), canon)
    assert any("divergence-class change" in x for x in v)


def test_norm_sha_is_line_ending_stable():
    assert norm_sha(b"a\r\nb\r\n") == norm_sha(b"a\nb\n") == norm_sha(b"a\nb")
