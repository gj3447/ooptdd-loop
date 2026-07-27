#!/usr/bin/env python3
"""Regenerate docs/SNAPSHOT_DRIFT_DECLARATION.json — the pinned allowlist for the
snapshot-drift gate (tests/test_snapshot_drift_gate.py).

This repo is a priority-anchored snapshot (Bitcoin block 958111, PROVENANCE/) that
intentionally diverges from the live canonical `ooptdd-loop`. The declaration pins,
at a specific canonical HEAD, exactly which paths diverge and with which content
hashes. The gate goes RED on any divergence not pinned here — running this script
is the *deliberate* act of re-declaring drift (POST_TIER1_ARC front A2: the lag is
a gate, not a doc note).

Usage: python scripts/gen_snapshot_drift_declaration.py --canonical /path/to/ooptdd-loop
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECL_REL = "docs/SNAPSHOT_DRIFT_DECLARATION.json"
DECLARATION = REPO / DECL_REL


def norm_sha(data: bytes) -> str:
    """Line-ending-normalized sha256 (same spirit as ooptdd's vendor manifest)."""
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = [ln.rstrip() for ln in text.split(b"\n")]
    return hashlib.sha256(b"\n".join(lines).rstrip(b"\n") + b"\n").hexdigest()


def tracked_files(root: Path) -> dict:
    out = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    files = {}
    for rel in out.decode().split("\0"):
        if not rel or rel == DECL_REL:   # the declaration cannot pin its own hash
            continue
        p = root / rel
        if p.is_file():
            files[rel] = norm_sha(p.read_bytes())
    return files


def head(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical", required=True, help="path to the live ooptdd-loop checkout")
    a = ap.parse_args()
    canon_root = Path(a.canonical).resolve()

    mark = tracked_files(REPO)
    canon = tracked_files(canon_root)

    mark_only = {p: s for p, s in sorted(mark.items()) if p not in canon}
    behind = {p: s for p, s in sorted(canon.items()) if p not in mark}
    modified = {p: {"mark": mark[p], "canonical": canon[p]}
                for p in sorted(set(mark) & set(canon)) if mark[p] != canon[p]}

    DECLARATION.write_text(json.dumps({
        "_comment": "Pinned snapshot-drift allowlist. Regenerate ONLY as a deliberate "
                    "re-declaration: python scripts/gen_snapshot_drift_declaration.py "
                    "--canonical <live ooptdd-loop>. The gate REDs on undeclared drift.",
        "canonical_repo": "gj3447/ooptdd-loop",
        "canonical_head": head(canon_root),
        "mark_head": head(REPO),
        "mark_only": mark_only,
        "behind": behind,
        "modified": modified,
    }, indent=2, sort_keys=False) + "\n")
    print(f"declared: mark_only={len(mark_only)} behind={len(behind)} modified={len(modified)}")
    print(f"canonical_head={head(canon_root)}")
    if behind:
        print("WARNING: non-empty BEHIND set — the snapshot lags canonical. "
              "Declaring it keeps the gate honest but the lag is now on the record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
