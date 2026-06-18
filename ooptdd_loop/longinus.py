"""Longinus binding — pierce the requirement down to the real emitting source.

A gate going GREEN proves *something* emitted the events. Longinus proves it was
the source you claimed: the named symbol exists in the named file, and the event
literal the requirement expects actually appears in that symbol's body. It also
captures a sha256 baseline so later drift (the code changed but the KG anchor
didn't) is detectable.

This is a deliberately lightweight, dependency-free check. For the full drift
engine (7-tuple ReferenceSite, ProcessPool scans, GED metrics) see
``GIT/bhgman_tool/engine/longinus_drift_audit/`` — this module produces the same
ReferenceSite shape so findings can be promoted there or into the KG.

**Git anchoring.** The binding is also pinned to git so the same anchor resolves to
the same code on *any* clone, on *any* machine: the source file's repository is
discovered with ``git rev-parse --show-toplevel`` (no caller-supplied absolute path),
and the ReferenceSite records the ``commit`` it was validated at, the content-addressed
``blob_oid`` (``git hash-object`` — machine-independent, the drift signal of record),
the ``remote`` URL, and the toplevel-relative ``repo_relpath``. All git fields are
best-effort: outside a git repo, or with git absent, they are ``None`` and the check
falls back to the plain ``sha256`` baseline — the offline invariant still holds.
"""
from __future__ import annotations

import ast
import hashlib
import os
import subprocess
from dataclasses import asdict, dataclass


@dataclass
class ReferenceSite:
    kg_anchor: str
    source_path: str
    symbol: str
    line_range: tuple[int, int] | None
    sha256: str
    emits: str
    bound: bool
    reason: str
    # Git anchoring (best-effort; None outside a git repo or when git is absent). These
    # are what make the anchor clone-portable: blob_oid is content-addressed so it matches
    # across machines for identical content, and commit/remote/repo_relpath identify
    # exactly which revision of which repo the baseline was taken from.
    commit: str | None = None
    blob_oid: str | None = None
    remote: str | None = None
    repo_relpath: str | None = None


def _git(args: list[str], cwd: str) -> str | None:
    """Run ``git <args>`` in ``cwd``; return stripped stdout, or ``None`` on any failure
    (git missing, not a repo, non-zero exit, timeout). Never raises — git is optional."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def git_identity(abs_path: str) -> dict:
    """Best-effort git anchoring for ``abs_path``.

    Discovers the file's repository from its own location (so the result is independent
    of the caller's CWD or any hardcoded root) and returns
    ``{toplevel, commit, blob_oid, remote, repo_relpath}`` — each ``None`` when the file
    is not inside a git work tree or git is unavailable. ``blob_oid`` is the *working-tree*
    content hash (``git hash-object``), i.e. the drift signal: it changes iff the bytes change.
    """
    d = os.path.dirname(abs_path) or "."
    out = {"toplevel": None, "commit": None, "blob_oid": None,
           "remote": None, "repo_relpath": None}
    top = _git(["rev-parse", "--show-toplevel"], d)
    if top is None:
        return out
    out["toplevel"] = top
    out["commit"] = _git(["rev-parse", "HEAD"], top)
    out["blob_oid"] = _git(["hash-object", abs_path], top)
    out["remote"] = _git(["config", "--get", "remote.origin.url"], top)
    try:
        out["repo_relpath"] = os.path.relpath(abs_path, top)
    except ValueError:  # different drive on Windows — no relative path exists
        out["repo_relpath"] = None
    return out


def _find_symbol(tree: ast.AST, symbol: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return node
    return None


def _emits_literal(node: ast.AST, literal: str) -> bool:
    """True iff ``literal`` appears inside a real string *constant* within ``node``'s
    AST subtree.

    AST-precise on purpose: a plain-text substring search (the old check) also matched
    the literal when it appeared only in a comment or as part of an unrelated identifier
    — both false GREENs that let an unbound symbol pass. Comments are absent from the AST
    and identifiers are ``ast.Name`` nodes, so neither can satisfy this. The match is
    containment (not equality) so a literal embedded in a larger message string — e.g.
    ``logger.info(f"[BL] {n} cycle.done")`` — still counts, while ``cycle.done`` in
    ``# emits cycle.done`` or in a variable named ``cycle_done`` does not.

    (Python sources only — Longinus parses with stdlib ``ast``. For multi-language
    binding, py-tree-sitter is the path; see the ooptdd-oss research seed
    ``seed-ooptdd-longinus-treesitter-20260618``.)
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and literal in sub.value:
            return True
    return False


def verify_binding(root: str, longinus) -> ReferenceSite:
    """Check that ``longinus.symbol`` exists in ``longinus.source`` and that its
    body references the ``must_emit`` event literal. Returns a ReferenceSite.

    ``root`` locates the file (``root/source``); the *git* identity (commit/blob/remote/
    repo_relpath) is then discovered from the file's own repository, so the resulting
    anchor is portable across clones and machines regardless of what ``root`` was.
    """
    path = os.path.join(root, longinus.source)
    git = git_identity(path) if os.path.isfile(path) else {}

    def site(line_range, sha, bound, reason) -> ReferenceSite:
        return ReferenceSite(
            kg_anchor=longinus.kg_anchor, source_path=longinus.source,
            symbol=longinus.symbol, line_range=line_range, sha256=sha,
            emits=longinus.must_emit, bound=bound, reason=reason,
            commit=git.get("commit"), blob_oid=git.get("blob_oid"),
            remote=git.get("remote"), repo_relpath=git.get("repo_relpath"),
        )

    if not os.path.isfile(path):
        return site(None, "", False, f"source file not found: {path}")
    src = open(path, encoding="utf-8").read()
    sha = hashlib.sha256(src.encode()).hexdigest()[:16]
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return site(None, sha, False, f"syntax error: {exc}")
    node = _find_symbol(tree, longinus.symbol)
    if node is None:
        return site(None, sha, False,
                    f"symbol '{longinus.symbol}' not defined in {longinus.source}")
    line_range = (node.lineno, getattr(node, "end_lineno", node.lineno))
    if not _emits_literal(node, longinus.must_emit):
        return site(line_range, sha, False,
                    f"'{longinus.symbol}' does not emit '{longinus.must_emit}' "
                    "(event literal absent from its body as a string constant)")
    return site(line_range, sha, True, "bound")


def write_to_kg(site: ReferenceSite, *, cycle_id: str) -> bool:
    """Best-effort: persist the ReferenceSite to the KG. No-op if neo4j env is
    absent. Kept optional so the loop runs fully offline.

    Honours the workspace convention: ReferenceSite 7-tuple anchored on disk
    sha256 (see Longinus v3.3). Requires NEO4J_URI/USER/PASSWORD in env.
    """
    import os as _os

    uri = _os.getenv("NEO4J_URI") or _os.getenv("NEO4J_URL")
    if not uri:
        return False
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return False
    user = _os.getenv("NEO4J_USER", "neo4j")
    pw = _os.getenv("NEO4J_PASSWORD", "")
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
        with drv.session() as s:
            s.run(
                """MERGE (rs:ReferenceSite:Longinus {kg_anchor:$a})
                   ON CREATE SET rs.sha256_baseline=$sha, rs.blob_oid_baseline=$blob
                   SET rs.source_path=$p, rs.symbol=$sym, rs.sha256=$sha,
                       rs.emits=$emits, rs.bound=$bound, rs.cycle_id=$cid,
                       rs.commit=$commit, rs.blob_oid=$blob, rs.remote=$remote,
                       rs.repo_relpath=$relpath, rs.last_validated=datetime()""",
                a=site.kg_anchor, p=site.source_path, sym=site.symbol, sha=site.sha256,
                emits=site.emits, bound=site.bound, cid=cycle_id,
                commit=site.commit, blob=site.blob_oid, remote=site.remote,
                relpath=site.repo_relpath,
            )
        drv.close()
        return True
    except Exception:
        return False


def as_dict(site: ReferenceSite) -> dict:
    return asdict(site)
