"""Longinus binding — pierce the requirement down to the real emitting source.

A gate going GREEN proves *something* emitted the events. Longinus proves it was
the source you claimed: the named symbol exists in the named file, and the event
literal the requirement expects actually appears — either in that symbol's own body
**or in a function it transitively calls** (the realistic case where an entry point
delegates emission to a helper). It also captures a sha256 baseline so later drift
(the code changed but the KG anchor didn't) is detectable.

Three layers of evidence, strongest first:

  1. **static reachability** — the must_emit literal appears as a string *constant*
     in the symbol's call-graph (its own AST subtree, or a callee's, resolved across
     modules under ``root``). The ``via`` chain records the path symbol→…→emitter.
  2. **runtime reachability** (optional) — when a coverage line-map is supplied, the
     exact line that emits the literal is checked to have *executed* under the run.
     ``runtime_reached`` reports it; ``require_runtime=True`` makes an un-executed
     emitter fail the binding (a literal that exists but never ran is not proof).
  3. **git anchoring** — the binding is pinned to git so the same anchor resolves to
     the same code on any clone (commit / content-addressed blob_oid / remote /
     repo_relpath); all best-effort and ``None`` outside a repo.

For the full drift engine (7-tuple ReferenceSite, ProcessPool scans, GED metrics) see
``GIT/bhgman_tool/engine/longinus_drift_audit/`` — this module produces the same
ReferenceSite shape so findings can be promoted there or into the KG. (Python sources
only — Longinus parses with stdlib ``ast``. For multi-language binding, py-tree-sitter
is the path; see seed ``seed-ooptdd-longinus-treesitter-20260618``.)
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from dataclasses import asdict, dataclass

_MAX_DEPTH = 8  # call-graph recursion bound — guards mutual recursion / deep trees

# An event *name* is an identifier-like token (``payment_authorized``, ``cycle.done``),
# never a human sentence. We use that to tell a structured emission (the literal is the
# event token, or a dict value under an event key) from a free-text log line (the literal
# is buried in a whitespace-bearing message passed to a logger).
_EVENT_KEYS = {"event", "type", "name", "msg_type", "event_type"}
_TOKEN_RE = re.compile(r"^[A-Za-z_][\w.\-:]*$")


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
    # Reachability evidence (added when the literal is emitted via the call graph and/or
    # checked against runtime coverage). `via` is the symbol chain entry→…→emitter
    # (empty when the entry symbol emits directly); emit_path/emit_line locate the actual
    # emitting string constant; runtime_reached is True/False when a coverage map was
    # supplied (None otherwise).
    via: tuple[str, ...] = ()
    emit_path: str | None = None
    emit_line: int | None = None
    runtime_reached: bool | None = None


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


def _emit_line(node: ast.AST, literal: str) -> int | None:
    """Line of the first *executable* string constant inside ``node`` containing ``literal``
    (None if none). AST-precise: comments are absent from the AST and identifiers are
    ``ast.Name`` nodes, so neither can satisfy this. A string constant that is the whole
    *value* of an expression statement (``ast.Expr``) is excluded too: that is a docstring
    or a dead bare-string literal — it has no runtime effect and so can never emit. This
    shuts the forgery "emit the event somewhere unreachable, then just name it in the
    claimed symbol's docstring". Containment (not equality) so a literal embedded in a larger
    message string still counts (e.g. ``logger.info(f"[BL] {n} cycle.done")``), while
    ``cycle.done`` in a comment, a docstring, or a variable named ``cycle_done`` does not."""
    # docstrings + dead bare-string statements: value of an Expr, zero runtime effect.
    dead = {
        id(s.value) for s in ast.walk(node)
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
        and isinstance(s.value.value, str)
    }
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and literal in sub.value and id(sub) not in dead):
            return getattr(sub, "lineno", None)
    return None


def _emits_literal(node: ast.AST, literal: str) -> bool:
    """True iff ``literal`` appears inside a real string constant within ``node``'s subtree."""
    return _emit_line(node, literal) is not None


# ── call-graph resolution (intra-module + best-effort cross-module under root) ──

def _module_defs(tree: ast.AST) -> dict[str, ast.AST]:
    """Index every function/method def in a module by name (best-effort: last wins on a
    name clash). Enough to resolve a callee name to a body to descend into."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _import_modules(tree: ast.AST) -> dict[str, str]:
    """Map a local name to the dotted module it came from, for ``from mod import name``
    (``name`` may be aliased). Used to follow a call into another module under ``root``."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


def _resolve_module_file(root: str, module: str) -> str | None:
    """Resolve a dotted module to a file under ``root`` (``a.b`` → ``a/b.py`` or
    ``a/b/__init__.py``). Best-effort, root-relative only — never escapes ``root``."""
    rel = module.replace(".", os.sep)
    for cand in (os.path.join(root, rel + ".py"), os.path.join(root, rel, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


def _callee_names(node: ast.AST) -> list[str]:
    """Names called inside ``node`` — ``f()`` → ``f``; ``obj.m()`` / ``mod.f()`` → the
    trailing attribute. Resolution by name is deliberately loose (we descend into any def
    of that name under root); it can over-approximate reachability, which is the safe
    direction for a binding check (it never invents a literal that isn't in some body)."""
    names: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                names.append(f.id)
            elif isinstance(f, ast.Attribute):
                names.append(f.attr)
    return names


def _parse(path: str) -> ast.AST | None:
    try:
        return ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return None


def _find_emit(root: str, path: str, symbol: str, literal: str,
               *, depth: int, visited: set) -> tuple[list[str], str, int] | None:
    """DFS the call graph from ``symbol`` in ``path`` looking for a string constant that
    contains ``literal``. Returns ``(via_chain, emit_path, emit_line)`` for the first
    emitter found, or ``None``. ``via_chain`` is the symbol path entry→…→emitter."""
    key = (path, symbol)
    if depth < 0 or key in visited:
        return None
    visited.add(key)
    tree = _parse(path)
    if tree is None:
        return None
    node = _find_symbol(tree, symbol)
    if node is None:
        return None
    line = _emit_line(node, literal)
    if line is not None:
        return ([symbol], path, line)
    # not emitted here — descend into resolvable callees (same module first, then imports)
    defs = _module_defs(tree)
    imports = _import_modules(tree)
    for callee in _callee_names(node):
        if callee == symbol:
            continue  # direct self-recursion adds nothing
        if callee in defs:
            found = _find_emit(root, path, callee, literal, depth=depth - 1, visited=visited)
        elif callee in imports:
            mod_file = _resolve_module_file(root, imports[callee])
            found = (_find_emit(root, mod_file, callee, literal, depth=depth - 1,
                                visited=visited) if mod_file else None)
        else:
            found = None
        if found is not None:
            chain, emit_path, emit_line = found
            return ([symbol, *chain], emit_path, emit_line)
    return None


# ── runtime reachability via a coverage line-map ───────────────────────────────

def load_coverage(path: str) -> dict[str, set[int]]:
    """Load a coverage.py data file into ``{abs_file_path: {executed_line, ...}}``.

    Requires the ``coverage`` package (an optional extra). Returns ``{}`` if it is absent
    or the file can't be read — runtime reachability then stays unknown (never a failure).
    """
    try:
        from coverage import CoverageData
    except ImportError:
        return {}
    try:
        data = CoverageData(basename=path)
        data.read()
        return {os.path.abspath(f): set(data.lines(f) or ()) for f in data.measured_files()}
    except Exception:
        return {}


def _runtime_reached(cov_lines: dict[str, set[int]] | None,
                     emit_path: str | None, emit_line: int | None) -> bool | None:
    """Did ``emit_line`` of ``emit_path`` execute, per the coverage map? ``None`` when no
    map was supplied (unknown), else a real True/False."""
    if not cov_lines or emit_path is None or emit_line is None:
        return None
    return emit_line in cov_lines.get(os.path.abspath(emit_path), set())


def verify_binding(root: str, longinus, *, cov_lines: dict[str, set[int]] | None = None,
                   require_runtime: bool = False) -> ReferenceSite:
    """Check that ``longinus.symbol`` exists in ``longinus.source`` and that its call graph
    emits the ``must_emit`` event literal. Returns a :class:`ReferenceSite`.

    ``root`` locates the file (``root/source``) and bounds cross-module call resolution;
    the *git* identity is discovered from the file's own repository, so the anchor is
    portable across clones regardless of ``root``. Supply ``cov_lines`` (e.g. from
    :func:`load_coverage`) to also record whether the emitting line executed; with
    ``require_runtime=True`` an un-executed emitter makes the binding unbound.
    """
    path = os.path.join(root, longinus.source)
    git = git_identity(path) if os.path.isfile(path) else {}

    def site(line_range, sha, bound, reason, *, via=(), emit_path=None, emit_line=None,
             runtime_reached=None) -> ReferenceSite:
        return ReferenceSite(
            kg_anchor=longinus.kg_anchor, source_path=longinus.source,
            symbol=longinus.symbol, line_range=line_range, sha256=sha,
            emits=longinus.must_emit, bound=bound, reason=reason,
            commit=git.get("commit"), blob_oid=git.get("blob_oid"),
            remote=git.get("remote"), repo_relpath=git.get("repo_relpath"),
            via=tuple(via), emit_path=emit_path, emit_line=emit_line,
            runtime_reached=runtime_reached,
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

    found = _find_emit(root, path, longinus.symbol, longinus.must_emit,
                       depth=_MAX_DEPTH, visited=set())
    if found is None:
        return site(line_range, sha, False,
                    f"'{longinus.symbol}' does not emit '{longinus.must_emit}' "
                    "(event literal absent from its body and its reachable callees "
                    "as a string constant)")
    chain, emit_path, emit_line = found
    via = tuple(chain[1:])  # drop the entry symbol itself; keep the path to the emitter
    reached = _runtime_reached(cov_lines, emit_path, emit_line)
    if require_runtime and reached is False:
        return site(line_range, sha, False,
                    f"'{longinus.must_emit}' is emitted (via {'->'.join(chain)}) but that "
                    f"line never executed under the run (runtime-unreachable)",
                    via=via, emit_path=emit_path, emit_line=emit_line, runtime_reached=False)
    reason = "bound" if not via else f"bound via {'->'.join(chain)}"
    return site(line_range, sha, True, reason, via=via, emit_path=emit_path,
                emit_line=emit_line, runtime_reached=reached)


def _dict_event_values(tree: ast.AST) -> set[str]:
    """String values that appear under an event-naming key in any dict literal —
    ``{"event": "payment_authorized"}`` contributes ``payment_authorized``."""
    out: set[str] = set()
    for d in ast.walk(tree):
        if not isinstance(d, ast.Dict):
            continue
        for k, v in zip(d.keys, d.values, strict=True):
            if (isinstance(k, ast.Constant) and k.value in _EVENT_KEYS
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                out.add(v.value)
    return out


def emission_kind(root: str, longinus) -> str:
    """Classify *how* the bound source emits ``must_emit`` — the code-level half of the
    "structured events only" methodology rule.

        ``structured`` the literal is emitted as an event token: a standalone string
                       constant equal to it, or a dict value under an event key
                       (``{"event": "..."}``) — i.e. a real structured event.
        ``free_text``  the literal only ever appears buried inside a human, whitespace-
                       bearing message string (a ``logger.info("... done")``-style line).
        ``absent``     the literal is not emitted by the bound symbol's call graph at all.

    This is genuine source analysis (AST over the reachable bodies), not a spec-shape
    check: it catches an implementation that "logs" the event as prose instead of
    emitting a structured, queryable record.
    """
    site = verify_binding(root, longinus)
    if not site.bound or site.emit_path is None:
        return "absent"
    tree = _parse(site.emit_path)
    if tree is None:
        return "absent"
    literal = longinus.must_emit
    if literal in _dict_event_values(tree):
        return "structured"
    free_text = False
    for sub in ast.walk(tree):
        if not (isinstance(sub, ast.Constant) and isinstance(sub.value, str)):
            continue
        val = sub.value
        if literal not in val:
            continue
        if val.strip() == literal and _TOKEN_RE.match(val.strip()):
            return "structured"        # the literal IS the event token
        free_text = True               # only seen inside a larger message string
    return "free_text" if free_text else "absent"


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
                       rs.repo_relpath=$relpath, rs.via=$via, rs.runtime_reached=$reached,
                       rs.last_validated=datetime()""",
                a=site.kg_anchor, p=site.source_path, sym=site.symbol, sha=site.sha256,
                emits=site.emits, bound=site.bound, cid=cycle_id,
                commit=site.commit, blob=site.blob_oid, remote=site.remote,
                relpath=site.repo_relpath, via=list(site.via),
                reached=site.runtime_reached,
            )
        drv.close()
        return True
    except Exception:
        return False


def as_dict(site: ReferenceSite) -> dict:
    return asdict(site)
