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
"""
from __future__ import annotations

import ast
import hashlib
import os
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


def _find_symbol(tree: ast.AST, symbol: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return node
    return None


def verify_binding(root: str, longinus) -> ReferenceSite:
    """Check that ``longinus.symbol`` exists in ``longinus.source`` and that its
    body references the ``must_emit`` event literal. Returns a ReferenceSite."""
    path = os.path.join(root, longinus.source)
    if not os.path.isfile(path):
        return ReferenceSite(longinus.kg_anchor, longinus.source, longinus.symbol,
                             None, "", longinus.must_emit, False,
                             f"source file not found: {path}")
    src = open(path, encoding="utf-8").read()
    sha = hashlib.sha256(src.encode()).hexdigest()[:16]
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return ReferenceSite(longinus.kg_anchor, longinus.source, longinus.symbol,
                             None, sha, longinus.must_emit, False, f"syntax error: {exc}")
    node = _find_symbol(tree, longinus.symbol)
    if node is None:
        return ReferenceSite(longinus.kg_anchor, longinus.source, longinus.symbol,
                             None, sha, longinus.must_emit, False,
                             f"symbol '{longinus.symbol}' not defined in {longinus.source}")
    seg = ast.get_source_segment(src, node) or ""
    line_range = (node.lineno, getattr(node, "end_lineno", node.lineno))
    if longinus.must_emit not in seg:
        return ReferenceSite(longinus.kg_anchor, longinus.source, longinus.symbol,
                             line_range, sha, longinus.must_emit, False,
                             f"'{longinus.symbol}' does not emit '{longinus.must_emit}' "
                             "(event literal absent from its body)")
    return ReferenceSite(longinus.kg_anchor, longinus.source, longinus.symbol,
                         line_range, sha, longinus.must_emit, True, "bound")


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
                   SET rs.source_path=$p, rs.symbol=$sym, rs.sha256=$sha,
                       rs.emits=$emits, rs.bound=$bound, rs.cycle_id=$cid,
                       rs.last_validated=datetime()""",
                a=site.kg_anchor, p=site.source_path, sym=site.symbol, sha=site.sha256,
                emits=site.emits, bound=site.bound, cid=cycle_id,
            )
        drv.close()
        return True
    except Exception:
        return False


def as_dict(site: ReferenceSite) -> dict:
    return asdict(site)
