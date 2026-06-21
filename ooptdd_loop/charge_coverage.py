"""L6 — execution-path (*charge*) coverage: a partial, honest close on the un-emitted-path gap.

The pseudo-oracle ceiling (PROM08): a gate can only judge events the system *emitted*. A code path
that ran but emitted nothing — a swallowed exception, a wrong ``cid``, a silent ``return`` before the
ship — is invisible to the store and therefore to the gate. It is green theater the lattice cannot see.

This module narrows that blind spot for the paths a run actually *executes*. While the in-process
target runs we measure it under ``coverage.py``; afterwards we AST-locate the *emit sites* in the
target module (calls that ship an event to the store, or structured log calls) and cross-check:

    executed emit site  +  its event name NOT in the store   ->  a CHARGE GAP

i.e. "this line ran and looks like it should have emitted ``X``, but ``X`` never arrived". That is
exactly the silent-drop class. The report is **advisory** — it never changes ``done`` / ``complete``;
it hands the agent leads, not verdicts.

Honest limits, stated loudly so the green is not over-read:
  * EXECUTED paths only. A wrong branch the run never takes is still outside the lattice (Rice).
  * Name-based heuristic. We flag a site only when an event name is *statically* extractable; dynamic
    names (f-strings, variables) are reported as *opaque*, not as gaps.
  * Scope = the target's entry module. Emit sites in other modules are not measured here.
  * Optional dependency. No ``coverage.py`` (or the env flag off) => a no-op report; the loop never
    crashes and never blocks on it.

Enable with ``OOPTDD_CHARGE_COVERAGE=1`` (and ``pip install coverage``).
"""
from __future__ import annotations

import ast
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Calls that ship a structured event to the store. The first positional may itself be the event
# (``emit("charge.ok")`` / ``write({"event": "charge.ok"})``), so positional extraction is allowed.
_STORE_EMITS = {"write", "emit", "ship", "record", "send", "ingest", "event", "publish"}
# stdlib-logging-style calls. Captured into the store by local_capture, so they ARE emits — but their
# first positional is the human message, never the event id, so positional extraction is refused.
_LOG_LEVELS = {"info", "warning", "warn", "error", "debug", "critical", "exception", "log"}
# keys under which an event name hides, matching how selector_gates reads records back.
_NAME_KEYS = ("event", "event_type", "name", "type")
_DICT_KWARGS = ("extra", "fields", "attrs", "attributes")


def charge_enabled() -> bool:
    return str(os.getenv("OOPTDD_CHARGE_COVERAGE", "")).strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- AST: emit-site detection

def _from_dict(node: ast.Dict) -> str | None:
    for key, val in zip(node.keys, node.values):
        if (isinstance(key, ast.Constant) and key.value in _NAME_KEYS
                and isinstance(val, ast.Constant) and isinstance(val.value, str)):
            return val.value
    return None


def _iter_dicts(node: ast.expr):
    """The dict literal(s) an arg carries: a bare dict, or dicts inside a list/tuple/set —
    so ``ship([{"event": "a"}, {"event": "b"}])`` (the codebase idiom) is seen, not just
    ``write({"event": "a"})``."""
    if isinstance(node, ast.Dict):
        yield node
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            if isinstance(elt, ast.Dict):
                yield elt


def _event_names(call: ast.Call, *, allow_positional: bool) -> list[str]:
    """Every STATIC event name an emit call carries (a list because one ``ship([...])`` may
    emit several). Empty when the name is only knowable at runtime (dynamic / via a wrapper)."""
    names: list[str] = []
    for arg in call.args:                       # dict, or list/tuple of dicts
        for d in _iter_dicts(arg):
            got = _from_dict(d)
            if got:
                names.append(got)
    for kw in call.keywords:                    # event=/name=, or extra={"event": "x"}
        if (kw.arg in _NAME_KEYS and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)):
            names.append(kw.value.value)
        elif kw.arg in _DICT_KWARGS and isinstance(kw.value, ast.Dict):
            got = _from_dict(kw.value)
            if got:
                names.append(got)
    if allow_positional and not names:          # emit("ship.done") — but never a log message
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.append(arg.value)
                break
    return names


def emit_sites(path: str) -> list[tuple[int, str | None]]:
    """``[(lineno, event_name_or_None), ...]`` for every emit-looking call in ``path``.

    One call with several static event names yields several entries; a call whose name is not
    statically knowable yields a single ``(lineno, None)`` (opaque). Unparseable / unreadable
    files yield ``[]`` — detection is best-effort and never raises.
    """
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return []
    out: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        attr = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else None)
        if attr is None:
            continue
        low = attr.lower()
        if low in _STORE_EMITS:
            names = _event_names(node, allow_positional=True)
        elif low in _LOG_LEVELS:
            names = _event_names(node, allow_positional=False)
        else:
            continue
        if names:
            out.extend((node.lineno, name) for name in names)
        else:
            out.append((node.lineno, None))
    return out


def observed_types(events) -> set[str]:
    """Event names actually present in the store for this run (permissive: fewer false gaps)."""
    seen: set[str] = set()
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        for key in _NAME_KEYS:
            val = ev.get(key)
            if isinstance(val, str):
                seen.add(val)
    return seen


# --------------------------------------------------------------------------- coverage controllers

class _NullController:
    """A no-op controller: charge-coverage disabled, coverage missing, or measurement failed."""

    enabled = False
    measured_files: tuple = ()

    def __init__(self, note: str = ""):
        self.note = note

    def executed_lines(self, file: str) -> set[int]:
        return set()


class _CovController:
    def __init__(self, cov, files):
        self._cov = cov
        self.measured_files = [os.path.abspath(f) for f in files]
        self.enabled = True
        self.note = ""

    def executed_lines(self, file: str) -> set[int]:
        try:
            lines = self._cov.get_data().lines(os.path.abspath(file))
        except Exception:  # noqa: BLE001 — a coverage read failure must not break the loop
            return set()
        return set(lines or ())


@contextmanager
def coverage_session(files):
    """Measure ``files`` under coverage.py for the duration of the ``with`` body.

    Always yields a controller, so the SUT body runs whether or not measurement is active. Disabled
    flag, a missing ``coverage`` package, or any start failure degrade to a no-op controller carrying
    a ``note`` — the loop is never blocked on optional instrumentation.
    """
    files = [f for f in (files or []) if f]
    if not charge_enabled():
        yield _NullController()
        return
    if not files:
        yield _NullController(note="no target file to measure")
        return
    try:
        import coverage
    except ImportError:
        yield _NullController(
            note="coverage.py not installed; `pip install coverage` to enable charge-coverage")
        return
    cov = None
    try:
        cov = coverage.Coverage(data_file=None, include=[os.path.abspath(f) for f in files])
        cov.start()
    except Exception as exc:  # noqa: BLE001
        if cov is not None:
            try:
                cov.stop()
            except Exception:  # noqa: BLE001
                pass
        yield _NullController(note=f"coverage failed to start: {exc}")
        return
    controller = _CovController(cov, files)
    try:
        yield controller
    finally:
        try:
            cov.stop()
        except Exception:  # noqa: BLE001
            controller.note = "coverage stop failed"


# --------------------------------------------------------------------------- the report

@dataclass
class ChargeSite:
    file: str
    line: int
    event: str | None       # statically-known event name, or None (opaque / dynamic)
    executed: bool
    observed: bool          # event name present in the store (meaningful only when event is set)


@dataclass
class ChargeReport:
    enabled: bool
    measured: list[str] = field(default_factory=list)
    sites: list[ChargeSite] = field(default_factory=list)
    note: str = ""

    @property
    def gaps(self) -> list[ChargeSite]:
        """Executed emit sites whose statically-known event never arrived — the silent-drop leads."""
        return [s for s in self.sites if s.executed and s.event and not s.observed]

    @property
    def corroborated(self) -> list[ChargeSite]:
        return [s for s in self.sites if s.executed and s.event and s.observed]

    @property
    def opaque(self) -> list[ChargeSite]:
        """Executed emits we can't statically name (dynamic event id) — undecidable, not a gap."""
        return [s for s in self.sites if s.executed and not s.event]

    def summary(self) -> str:
        if not self.enabled:
            return "charge-coverage: off" + (f" — {self.note}" if self.note else "")
        gaps, ok, opaque = self.gaps, self.corroborated, self.opaque
        head = (f"charge-coverage (advisory, EXECUTED paths only): {len(ok)} emit sites corroborated, "
                f"{len(gaps)} executed-but-UNOBSERVED, {len(opaque)} opaque")
        out = [head]
        for s in gaps[:10]:
            out.append(f"     ⚡ {os.path.basename(s.file)}:{s.line} emits '{s.event}' — ran but never "
                       f"reached the store (silent drop / wrong cid / swallowed error?)")
        if len(gaps) > 10:
            out.append(f"     … +{len(gaps) - 10} more")
        out.append("     NOTE: name-based heuristic; un-executed wrong paths and dynamic event names "
                   "are out of scope. Leads to investigate, not gate failures.")
        return "\n".join(out)


def build_charge_report(controller, events) -> ChargeReport:
    """Cross-check executed emit sites against the events that actually arrived in the store."""
    if controller is None or not getattr(controller, "enabled", False):
        return ChargeReport(enabled=False, note=getattr(controller, "note", "") if controller else "")
    observed = observed_types(events)
    sites: list[ChargeSite] = []
    for file in controller.measured_files:
        executed = controller.executed_lines(file)
        for line, event in emit_sites(file):
            sites.append(ChargeSite(
                file=file, line=line, event=event,
                executed=line in executed,
                observed=bool(event) and event in observed,
            ))
    return ChargeReport(enabled=True, measured=list(controller.measured_files),
                        sites=sites, note=getattr(controller, "note", ""))
