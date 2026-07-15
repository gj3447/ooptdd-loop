"""The loop's independent stop controls, durable state, and fix containment.

``runner.run_until_complete`` drives run → judge → (if RED) fix → re-run. Left to itself
that loop trusts three things it should not:

* **the pass counter** — a fix command that burns an hour of wall-clock or $40 of agent
  spend inside a single pass is invisible to a budget counted in passes;
* **its own memory** — the transcript lives in RAM, so a crash restarts at pass 1 and
  repays every agent call already paid for;
* **the fix command** — it runs ``shell=True`` with the full inherited env and may write
  anywhere the OS user can.

This module is those three controls, as the classes the runner actually uses. It maps to
the PROM16 harness/loop-engineering criteria:

* ``LoopReason``        — S1: typed terminal states. Every stop the loop *itself decides* —
                          a spent budget, a hung fix, a stall, an escaped write — is one of
                          these rather than a traceback, and none of its budgets is
                          unbounded. It is not a blanket "no exception escapes": a
                          misconfiguration raises, and the system under test's own
                          exceptions propagate. See ``runner.run_until_complete``.
* ``LoopGuard``         — S1 (step ceiling + no-progress patience) and S5 (wall-clock and
                          spend kill-switch), all independent of the agent's self-judgment.
* ``DurableRunJournal`` — S4: an append-only JSONL record written as each pass completes,
                          and the replay that resumes a crashed run without repaying work.
* ``fix_env`` / ``audit_writeset`` / ``kill_process_tree`` — S7: containment of the fix
                          command's environment, write-set, and process tree.

stdlib only, on purpose: a guard that can fail because a dependency is missing is not a
guard. Fail-CLOSED is the rule throughout — an audit or a meter that cannot run STOPS the
loop, it never waves it through.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence


class LoopReason(str, Enum):
    """Why the loop stopped — a typed terminal state (S1) rather than a traceback, for every
    stop the loop itself decides.

    A ``str`` enum: ``run.loop_reason == "stalled"`` and ``json.dumps`` keep working on the
    original four values, which stay the wire vocabulary.

    Goal reached
        ``complete``            every requirement is DONE.
    Step ceiling / no progress (S1)
        ``single_pass``         the default one-pass budget is spent.
        ``max_passes``          the multi-pass budget is spent.
        ``stalled``             ``patience`` consecutive passes changed nothing.
    Resource kill-switch (S5)
        ``budget_time``         the wall-clock budget is spent.
        ``budget_spend``        the spend budget is spent — or the spend meter could not be
                                read, which stops the loop fail-closed.
        ``fix_timeout``         the fix command did not return within its bound and was
                                killed.
    Containment (S7)
        ``writeset_violation``  the fix wrote outside its declared paths — or the write
                                audit could not run, which stops the loop fail-closed.
    """

    COMPLETE = "complete"
    SINGLE_PASS = "single_pass"
    MAX_PASSES = "max_passes"
    STALLED = "stalled"
    BUDGET_TIME = "budget_time"
    BUDGET_SPEND = "budget_spend"
    FIX_TIMEOUT = "fix_timeout"
    WRITESET_VIOLATION = "writeset_violation"

    def __str__(self) -> str:  # so f"{reason}" renders the wire value, not "LoopReason.X"
        return self.value


# ── S1 + S5: the independent stop ─────────────────────────────────────────────
class LoopGuard:
    """Every bound on the loop, in one place, decided without asking the agent (S1 + S5).

    S1 — *step ceiling and no-progress*: ``budget`` passes maximum, and ``patience``
    consecutive passes that change nothing (an agent editing in circles) is a stall.

    S5 — *resource kill-switch*: ``max_seconds`` of wall-clock and ``max_spend`` of agent
    spend, both checked between passes and *before* invoking a fix, so the loop never pays
    for a step it has no budget for. ``fix_timeout`` additionally bounds a single fix
    invocation, so a fix that never returns cannot make the between-pass checks unreachable.

    The wall-clock is measured from :meth:`start`, i.e. per ``run_until_complete`` call.
    Resuming a journaled run (S4) restores the pass counter and the stall state, not the
    clock: ``max_seconds`` bounds *this* invocation, not the run's whole history.

    ``max_spend`` requires ``spend_fn`` — a spend budget with no meter to read would be a
    silent no-op, so it is a construction-time ``ValueError`` instead. ``spend_fn`` is
    injected (a file the agent updates, an API cost readback, …); this module never guesses
    what an agent costs.
    """

    def __init__(self, *, max_passes: int = 1, patience: int = 2,
                 max_seconds: float | None = None,
                 max_spend: float | None = None,
                 spend_fn: Callable[[], float] | None = None,
                 fix_timeout_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if max_spend is not None and spend_fn is None:
            raise ValueError(
                "max_spend needs spend_fn: a spend budget with no meter to read would "
                "silently never fire, which is worse than no budget at all"
            )
        if max_seconds is not None and max_seconds <= 0:
            raise ValueError(f"max_seconds must be > 0 (got {max_seconds!r})")
        if max_spend is not None and max_spend < 0:
            raise ValueError(f"max_spend must be >= 0 (got {max_spend!r})")
        if fix_timeout_s is not None and fix_timeout_s <= 0:
            raise ValueError(f"fix_timeout_s must be > 0 (got {fix_timeout_s!r})")
        self.budget = max(int(max_passes), 1)
        self.patience = int(patience)
        self.max_seconds = max_seconds
        self.max_spend = max_spend
        self.fix_timeout_s = fix_timeout_s
        self.stall = 0
        self.spent: float | None = None
        self.stop_note: str | None = None
        self._spend_fn = spend_fn
        self._clock = clock
        self._t0 = clock()

    # ── wall-clock ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Open the wall-clock window. Called once, when the loop begins."""
        self._t0 = self._clock()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._t0

    def remaining_seconds(self) -> float | None:
        """Wall-clock left, or None when unbudgeted. Never negative."""
        if self.max_seconds is None:
            return None
        return max(0.0, self.max_seconds - self.elapsed)

    def fix_timeout(self) -> float | None:
        """The bound for ONE fix invocation: the tighter of ``fix_timeout_s`` and the
        wall-clock left (S5/GAP-1b). None only when neither budget is set — i.e. only an
        explicitly unbudgeted loop may host an unbounded fix."""
        bounds = [b for b in (self.fix_timeout_s, self.remaining_seconds()) if b is not None]
        return min(bounds) if bounds else None

    # ── spend ─────────────────────────────────────────────────────────────────
    def spend(self) -> float | None:
        """Read the injected meter. Raises whatever the meter raises — the caller decides
        (``resource_stop`` turns a broken meter into a fail-closed stop)."""
        if self._spend_fn is None:
            return None
        spent = float(self._spend_fn())
        self.spent = spent
        return spent

    # ── the stops ─────────────────────────────────────────────────────────────
    def resource_stop(self) -> LoopReason | None:
        """The S5 kill-switch: wall-clock and spend, checked without the agent's consent.

        A meter that raises stops the loop with ``budget_spend`` — fail CLOSED: a budget we
        cannot measure must block, never wave the loop through."""
        if self.max_seconds is not None and self.elapsed >= self.max_seconds:
            self.stop_note = (f"wall-clock budget spent: {self.elapsed:.2f}s of "
                              f"{self.max_seconds}s")
            return LoopReason.BUDGET_TIME
        if self.max_spend is not None:
            try:
                spent = self.spend()
            except Exception as exc:  # noqa: BLE001 — any unreadable meter must fail closed
                self.stop_note = (f"spend meter unreadable ({exc!r}); stopping fail-closed "
                                  "rather than running an unmetered budget")
                return LoopReason.BUDGET_SPEND
            if spent is not None and spent >= self.max_spend:
                self.stop_note = f"spend budget spent: {spent} of {self.max_spend}"
                return LoopReason.BUDGET_SPEND
        return None

    def note_progress(self, progressed: bool, *, pass_no: int) -> None:
        """Record whether a pass changed the verdict. Only passes after the first can
        stall — the first pass has nothing to be identical to."""
        if pass_no > 1 and not progressed:
            self.stall += 1
        else:
            self.stall = 0

    @property
    def stalled(self) -> bool:
        return self.stall >= self.patience

    def step_stop(self, pass_no: int) -> LoopReason | None:
        """The S1 stop: the step ceiling first (the budget is the hard bound), then the
        no-progress stall."""
        if pass_no >= self.budget:
            return LoopReason.SINGLE_PASS if self.budget == 1 else LoopReason.MAX_PASSES
        if self.stalled:
            self.stop_note = f"{self.stall} consecutive passes changed nothing"
            return LoopReason.STALLED
        return None


def spend_file_reader(path: str | os.PathLike) -> Callable[[], float]:
    """A spend meter for callers with no cost API: a file holding cumulative spend, which
    the fix command (or a wrapper around it) updates as it goes (S5).

    A missing or empty file reads 0.0 — nothing has been spent yet. A file that exists but
    does not parse raises, and :meth:`LoopGuard.resource_stop` turns that into a fail-closed
    ``budget_spend`` stop: an unreadable meter must never silently disable the kill-switch.
    """
    p = Path(path)

    def read() -> float:
        if not p.exists():
            return 0.0
        raw = p.read_text(encoding="utf-8").strip()
        return float(raw) if raw else 0.0

    return read


# ── S4: durable state ─────────────────────────────────────────────────────────
class JournalCorruptionError(RuntimeError):
    """The journal cannot be replayed as written (S4).

    A programming/serialization error, not a transient one: resuming from a half-understood
    journal would silently restart at the wrong pass or with the wrong stall state, so it
    fails fast instead. A torn *last* line — the normal shape of a crash mid-append — is not
    corruption and is dropped on replay.
    """


@dataclass(frozen=True)
class JournalEntry:
    """One completed pass, as written to the journal the instant the pass is judged (S4).

    Deliberately the verdict only: it is appended *before* the fix for that pass runs, so a
    crash inside the fix resumes at the NEXT pass and re-measures the code on disk rather
    than re-paying the agent for an edit it may already have made.
    """

    run_id: str
    pass_no: int
    cid: str
    complete: bool
    n_done: int
    total: int
    red: tuple[str, ...]
    gates: tuple[dict, ...]
    progressed: bool
    stall: int
    state_key: str
    started_at: float
    ended_at: float

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict, *, source: str) -> JournalEntry:
        try:
            return cls(
                run_id=payload["run_id"], pass_no=int(payload["pass_no"]),
                cid=payload["cid"], complete=bool(payload["complete"]),
                n_done=int(payload["n_done"]), total=int(payload["total"]),
                red=tuple(payload.get("red") or ()),
                gates=tuple(payload.get("gates") or ()),
                progressed=bool(payload["progressed"]), stall=int(payload["stall"]),
                state_key=payload["state_key"],
                started_at=float(payload["started_at"]), ended_at=float(payload["ended_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalCorruptionError(f"{source}: not a journal entry ({exc})") from exc


@dataclass(frozen=True)
class ReplayState:
    """What a crashed run left behind: enough to resume the pass counter and the stall
    state, plus the last verdict it actually recorded (S4)."""

    entries: tuple[JournalEntry, ...] = ()

    @property
    def passes(self) -> int:
        """Passes already paid for. The resumed loop starts at ``passes + 1``."""
        return max((e.pass_no for e in self.entries), default=0)

    @property
    def stall(self) -> int:
        return self.entries[-1].stall if self.entries else 0

    @property
    def state_key(self) -> str | None:
        return self.entries[-1].state_key if self.entries else None

    @property
    def last(self) -> JournalEntry | None:
        return self.entries[-1] if self.entries else None


class DurableRunJournal:
    """The append-only JSONL record of a run, and the replay that resumes it (S4).

    One line per completed pass, ``fsync``-ed on write, keyed by ``run_id`` so several runs
    may share a file and a resume only ever picks up its own. Append-only: nothing is
    rewritten, so the file is the audit trail as well as the resume point.
    """

    def __init__(self, path: str | os.PathLike, run_id: str):
        self.path = Path(path)
        self.run_id = run_id

    def append(self, entry: JournalEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.as_dict(), ensure_ascii=False, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # the point of the journal is surviving the crash

    def replay(self) -> ReplayState:
        """Read this run's entries back. Raises :class:`JournalCorruptionError` on a
        malformed line, except a torn final line (a crash mid-append), which is dropped."""
        if not self.path.exists():
            return ReplayState()
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        entries: list[JournalEntry] = []
        for i, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                if i == len(lines) - 1 and not line.endswith("\n"):
                    break  # half-written last line == the crash we are resuming from
                raise JournalCorruptionError(f"{self.path}:{i + 1} is not JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise JournalCorruptionError(f"{self.path}:{i + 1} is not a JSON object")
            if payload.get("run_id") != self.run_id:
                continue  # another run's lines share the file; they are not ours to resume
            entries.append(JournalEntry.from_dict(payload, source=f"{self.path}:{i + 1}"))
        return ReplayState(tuple(sorted(entries, key=lambda e: e.pass_no)))


# ── S7: fix-command containment ───────────────────────────────────────────────
#: ``env_allowlist`` sentinel: inherit the FULL parent environment, as the fix command did
#: before the scrub existed. The migration path for a fix that needs ANTHROPIC_API_KEY /
#: OPENAI_API_KEY / cloud credentials and has not been given an explicit allowlist yet.
INHERIT_ALL = "*"

#: What a fix command gets when no allowlist is given: enough to *run* (a shell, a Python,
#: a temp dir, a locale) and nothing else. Deliberately excludes every credential-shaped
#: name — see ``fix_env`` for the back-compat break this represents.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "PYTHONPATH", "PYTHONHASHSEED", "VIRTUAL_ENV", "SYSTEMROOT",
)


def fix_env(injected: dict[str, str], *,
            allowlist: str | Sequence[str] | None = None,
            environ: dict[str, str] | None = None) -> dict[str, str]:
    """Build the fix command's environment from an explicit allowlist (S7).

    The loop invokes an *agent* through a shell. Handing it the parent's whole environment
    hands it every credential, token and cloud role the loop happens to be holding, for a
    job whose only declared need is to edit source from an RCA.

    **This is a deliberate behavior break.** Before the scrub, the fix command inherited the
    full parent env; with ``allowlist=None`` it now inherits ``DEFAULT_ENV_ALLOWLIST`` plus
    ``injected`` (the loop's own ``OOPTDD_*`` contract) and nothing else. A fix command that
    needs ``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` must now say so:

        fix_env(vars, allowlist=[*DEFAULT_ENV_ALLOWLIST, "ANTHROPIC_API_KEY"])
        fix_env(vars, allowlist=INHERIT_ALL)   # or opt out entirely (pre-scrub behavior)

    ``allowlist`` here is LITERAL and REPLACES the defaults: the names given are the whole
    allowlist. ``allowlist=["ANTHROPIC_API_KEY"]`` therefore yields an env with that key and
    no PATH — which a fix invoked through a shell cannot run in (the shell falls back to a
    built-in PATH and typically cannot find its own agent). That is why the first form above
    splices ``DEFAULT_ENV_ALLOWLIST`` in explicitly. The CLI's ``--fix-env-allow`` is the
    additive form of the same thing and does that splice for you (``cli._fix_env_allowlist``),
    so ``--fix-env-allow ANTHROPIC_API_KEY`` and the first line above name the same env.

    ``injected`` always wins: the ``OOPTDD_*`` variables are the loop's contract with the
    fix, not something the caller's environment may shadow.
    """
    src = os.environ if environ is None else environ
    if allowlist is None:
        names: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    elif isinstance(allowlist, str):
        names = (allowlist,)  # a bare sentinel/name, not a sequence of characters
    else:
        names = tuple(allowlist)
    env = dict(src) if INHERIT_ALL in names else {k: v for k, v in src.items() if k in names}
    env.update(injected)
    return env


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a timed-out fix and everything it spawned (S7).

    ``shell=True`` means the direct child is a shell; killing it alone orphans the agent it
    launched. The fix is started with ``start_new_session=True``, so the whole session can
    be signalled at once. Falls back to killing the direct child when the platform has no
    process groups.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (AttributeError, OSError):  # no process groups, or it is already gone
        try:
            proc.kill()
        except OSError:
            pass


@dataclass(frozen=True)
class WriteAudit:
    """What the fix touched, and whether all of it was declared (S7).

    ``ok`` is False both when something landed outside the allowlist and when the audit
    could not run at all (``error`` set) — an audit that cannot run must BLOCK.
    """

    ok: bool
    write_set: tuple[str, ...] = ()
    outside: tuple[str, ...] = ()
    head_moved: bool = False
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"write audit could not run: {self.error}"
        if self.outside:
            return "fix wrote outside its declared paths: " + ", ".join(self.outside)
        return f"fix write-set ({len(self.write_set)} path(s)) is inside the declared paths"


def _git(root: str | os.PathLike, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout


def git_head(root: str | os.PathLike) -> str | None:
    """The commit ``root`` is on, or None when there is no repo / no commit yet.

    Recorded *before* a fix runs so :func:`audit_writeset` can diff against it: a fix that
    commits its own writes leaves a clean ``git status`` and would otherwise audit clean.
    """
    try:
        rc, out = _git(root, "rev-parse", "HEAD")
    except OSError:
        return None
    return (out.strip() or None) if rc == 0 else None


def _porcelain_paths(out: str) -> list[str]:
    """Paths from ``git status --porcelain -z`` — NUL-separated, so never shell-quoted.
    A rename/copy entry is followed by its source path in the next field."""
    fields = [f for f in out.split("\0") if f]
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in xy or "C" in xy) and i < len(fields):
            paths.append(fields[i])
            i += 1
    return paths


def _prefixes(root: str | os.PathLike, allowlist: Iterable[str]) -> list[str]:
    out = []
    for entry in allowlist:
        p = Path(entry).expanduser()
        if not p.is_absolute():
            p = Path(root) / p
        out.append(os.path.realpath(str(p)))
    return out


def _inside(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == pre or path.startswith(pre.rstrip(os.sep) + os.sep) for pre in prefixes)


def audit_writeset(root: str | os.PathLike, allowlist: Iterable[str], *,
                   pre_head: str | None = None) -> WriteAudit:
    """Check what the fix wrote against the paths it was allowed to write (S7).

    **What is ENFORCED.** Every write git can see in ``root``'s work tree:

    * ``git status --porcelain --ignored=matching --untracked-files=all`` — modified, staged,
      untracked *and* gitignored paths. ``--ignored=matching`` is deliberate: a fix could
      otherwise park a write in any gitignored path and audit clean. ``-uall`` reports
      untracked writes per file rather than collapsing them into a directory entry; an
      ignored directory that matches an ignore pattern is still reported as the directory,
      which is coarser but still outside any allowlist that does not name it.
    * ``git diff --name-only <pre_head>`` — every path whose content differs from the
      pre-fix commit, so a fix that ``git commit``s its own writes (leaving ``git status``
      clean) is still caught.
    * HEAD movement itself, including the case where a fix creates the repo's first commit
      and there is no ``pre_head`` to diff against — which is therefore unauditable, and so
      blocks.

    **What is NOT enforced — advisory only.** Writes git cannot see:

    * anything *outside* the git work tree (``/tmp``, ``$HOME``, another checkout, a
      network call). Nothing here observes those.
    * a work tree that is not a git repository at all — that BLOCKS rather than passes, but
      it is not confinement either.

    So the honest claim is "no write that git can see landed outside the declared paths",
    NOT "the fix only wrote inside the declared paths". Real confinement of an untrusted
    fix needs an OS sandbox (container, seccomp, a scratch worktree); this audit is a
    tripwire, and the env scrub above is the credential half of the same job.

    **Consequence of ``--ignored=matching``.** The audited write-set is intentionally
    over-inclusive: it holds pre-existing dirt and build artifacts too, because attributing
    each path to *this* fix is not something git can answer. Anything the run legitimately
    produces — ``__pycache__/`` from an ``in_process`` target, ``.venv/``, ``.ooptdd/`` —
    must therefore be in the allowlist alongside the source the fix may edit. That is the
    price of the direction the errors point: over-inclusive raises false alarms, and
    under-inclusive misses real escapes.
    """
    try:
        rc, top = _git(root, "rev-parse", "--show-toplevel")
    except OSError as exc:
        return WriteAudit(ok=False, error=f"git is not available ({exc})")
    if rc != 0 or not top.strip():
        return WriteAudit(ok=False, error=f"{root} is not inside a git work tree, so the "
                                          "fix's write-set cannot be audited")
    repo = Path(top.strip())
    rc, out = _git(root, "status", "--porcelain", "--ignored=matching",
                   "--untracked-files=all", "-z")
    if rc != 0:
        return WriteAudit(ok=False, error="`git status` failed, so the fix's write-set "
                                          "cannot be audited")
    touched = set(_porcelain_paths(out))
    if pre_head:
        rc, out = _git(root, "diff", "--name-only", "-z", pre_head)
        if rc != 0:
            return WriteAudit(ok=False, error=f"`git diff {pre_head}` failed, so a fix that "
                                              "committed its writes cannot be audited")
        touched |= {p for p in out.split("\0") if p}
    post_head = git_head(root)
    head_moved = (pre_head or None) != (post_head or None)
    write_set = tuple(sorted(os.path.realpath(str(repo / p)) for p in touched))
    if head_moved and not pre_head:
        return WriteAudit(ok=False, write_set=write_set, head_moved=True,
                          error=f"the fix moved HEAD to {post_head} with no pre-fix commit "
                                "to diff against, so its write-set cannot be audited")
    prefixes = _prefixes(root, allowlist)
    outside = tuple(p for p in write_set if not _inside(p, prefixes))
    if head_moved and not write_set:
        # History moved but no content differs from pre_head: nothing escaped by path, yet a
        # path allowlist cannot authorize a history rewrite. Report it rather than pass.
        outside = (f"<git HEAD moved {pre_head} -> {post_head}>",)
    return WriteAudit(ok=not outside, write_set=write_set, outside=outside,
                      head_moved=head_moved)
