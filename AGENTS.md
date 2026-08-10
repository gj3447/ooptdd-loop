# Repository Contract

Instructions for coding agents and new contributors. This file is the single
source; `CLAUDE.md` points here. For what the loop *is*, read `AGENT_LOOP.md`.

`ooptdd-loop` exists to make "the agent says it's done" untrustworthy by
construction. A requirement is done when the log store proves it, not when the
suite is green. Do not weaken a gate to reach a green.

## Commands

- Install: `pip install "git+https://github.com/gj3447/ooptdd.git@main"` then
  `pip install -e ".[dev]"`. **`ooptdd` is a sibling repo and is not on PyPI** —
  a plain `pip install -e ".[dev]"` gives you a broken environment.
- Fast validation: `.venv/bin/python -m ruff check .` then
  `.venv/bin/python -m pytest -q tests/test_pytest_plugin.py tests/test_otel.py`
- Full validation: `scripts/verify_ooptdd.sh`
- Full validation incl. logserver/OpenObserve: `scripts/verify_ooptdd.sh --include-external`
- Targeted test: `.venv/bin/python -m pytest -q tests/<file>.py`

`scripts/verify_ooptdd.sh` is the completion gate: ruff, focused runtime/OTel
tests, the full suite, CLI tools registry, harness profile, methodology
validation, memory-backed example specs, golden save/diff, MCP metadata, MCP
stdio roundtrip, and Claude/Codex MCP config generation.

Use `python -m ruff`, never bare `ruff`. Bare `ruff` only resolves with the venv
activated, so the script used to report "verification passed" for whoever
guessed that step and `ruff: command not found` for everyone else.

## What CI does and does not cover

`.github/workflows/ci.yml` runs **only `pytest -q`**, on Python 3.11/3.12/3.13.

Everything else — lint, the CLI and MCP smokes, spec validation, golden diffs —
exists **only in `scripts/verify_ooptdd.sh`, which CI never calls.** A green CI
badge therefore says nothing about those. Run the script before claiming done.

## The 2026-08-10 upstream skew — solved AND fixed

ooptdd a53e844 (2026-08-07, "generic functional framework") changed five
contracts at once: per-instance memory stores (no module-global sharing),
integer-only query window bounds, no ambient os.environ reads in the engine,
immutable mappingproxy query views, and tuple-frozen ontology fields. This
repo silently depended on all five; a fresh  install went from
238 passed to 33 failed with no commit here changing.

Fixed in 54246df / ec0daa6 / d5d7ef1. The load-bearing piece is
**ooptdd_loop/backends.py** — the composition root that owns the one
process-level MemoryStore. Every backend acquisition in the package AND in the
test suite goes through ; two raw 
calls silently stop sharing evidence, which is the exact bug class the skew
was made of. Suite against current ooptdd main: **238 passed, 4 skipped, 0
failed** — the pre-refactor green, restored without touching one assertion.

Rules that keep it fixed:

- Never call  directly from loop code or tests;
  go through . For an isolated world, pass your
  own  explicitly.
- Argument-less  is a no-op upstream; use
   in fixtures.
- Anything crossing the xdist workeroutput boundary must be materialised into
  plain dicts first (); upstream query views are immutable
  proxies and  will stringify them whole.
## The ruff version trap

`pyproject.toml` declares `ruff>=0.4` with no upper bound, and **ruff expanded
its default rule set in later releases.** Measured on identical source: `ruff
0.4.10` reported 0 findings where `ruff 0.16.2` reported 82 (`I001`, `BLE001`,
`S110`, `PLW1510`, …). The lint gate moves under you with no commit.

Consequences:

- Do not `pip install -U ruff` in this venv casually. If you do, `ruff<0.5`
  restores the current gate.
- Do not "fix" a sudden wall of lint findings that appeared without a code
  change. Check `python -m ruff --version` first.
- The real fix is to pin ruff in `pyproject.toml` and adopt the new rules
  deliberately. That is a decision, not a cleanup — propose it, do not do it
  inside an unrelated change.

## Definition of Done

A task is complete only when:

1. The requirement exists as a trace gate plus a Longinus binding, written
   before the code satisfies it (the Red artifact).
2. `scripts/verify_ooptdd.sh` exits 0, with the three known-red tests either
   still failing for the documented reason or fixed on purpose.
3. No gate, spec, expected event set, or test was weakened to get there.
4. The final diff was reviewed for unrelated changes.

Adding a requirement, gate, or test needs no approval. **Relaxing an expected
event set, deleting a gate, or narrowing the CI matrix does.**

## Workflow

1. Read `AGENT_LOOP.md` and the nearest existing spec in `example/`.
2. State the intended behavior before editing.
3. Make the smallest coherent change.
4. Run the targeted test, then the fast validation pair.
5. Before claiming completion, run `scripts/verify_ooptdd.sh`.
6. Report which commands you ran and what they printed. Do not report a green
   you did not observe.

## Execution Budget

The loop is bounded by construction — every bound lives in `harness.LoopGuard`
and every stop it decides is a typed `LoopReason`:

- `--passes` (pass ceiling) and `--patience` (consecutive no-progress passes
  stop the loop as a stall);
- `--max-seconds` (wall-clock) and `--max-spend` + `--spend-file` (agent spend,
  fail-closed when the meter cannot be read);
- `--fix-timeout` — per-fix bound, **1800 s by default** (2026-08-10): a hung
  fix is killed with its whole process tree. `--fix-timeout 0` (API:
  `fix_timeout_s=None`) is the explicit unbounded opt-out;
- `--fix-write-allow` — write-set audit; a fix writing outside it stops the loop.

Do not arm an autonomous fix loop with every bound switched off. The measured
failure mode (2026-08-07..10) was 13-43 hour agent sessions whose every time
bound was opt-in — being bounded is the default here, and opting out is a
decision you state, not a state you drift into.
