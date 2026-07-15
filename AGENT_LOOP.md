# The agent loop — develop until the logs prove it, then bind it

`ooptdd-loop` is the loop that makes "the agent says it's done" untrustworthy by
construction. Both halves — writing the code *and* catching the harness's own
failures — run through the same machine verdict, so a requirement can only be
marked done when reality agrees.

## The loop

```
            ┌──────────────────────────────────────────────────────┐
            │  spec: requirements as trace gates + Longinus bindings │
            └──────────────────────────────────────────────────────┘
                                   │
   ┌───────────────┐   run    ┌────▼─────┐   poll store    ┌──────────────┐
   │  DEV AGENT    │ ───────► │  ooptdd  │ ──────────────► │  log store   │
   │ writes/edits  │          │  -loop   │ ◄────────────── │ (oo / memory)│
   └───────▲───────┘          └────┬─────┘  events arrive? └──────────────┘
           │                       │
           │  log-grounded RCA     │  per requirement:
           │  (NOT a guess)        │   gate GREEN?  +  Longinus bound?
           └───────────────────────┘
                                   │
                    all DONE ──────┴────── exit 0  ✅ requirements complete
```

1. **Declare** each requirement as a trace gate (the expected events) plus a
   Longinus binding (the source symbol that must emit them). This is the *Red*
   artifact — written before the code satisfies it.
2. **The dev agent writes code.** (Claude / Codex — whatever you drive it with.)
3. **`ooptdd-loop run` executes the code** and reads the store back. A requirement
   is GREEN only if its events actually arrived (positive arrival), and DONE only
   if it is also Longinus-bound to source that really exists and really emits them.
   *Both wings count:* the good events must arrive **and** no forbidden event may —
   an `absent:` rule (or the `OOPTDD_FORBID_ERRORS` default, which forbids
   `ERROR`/`CRITICAL` records for the cid) turns a green-but-erroring cycle RED and
   feeds the offending log lines back to the agent. Exempt known-benign ones with
   `allow_errors:`.
4. **RED comes back with a log-grounded RCA**, not a guess: what the store saw,
   what's missing, whether it's a missing event vs a count mismatch vs an
   unreachable store (which is `inconclusive`, never the code's fault).
5. **The agent fixes and re-runs.** Repeat until `ooptdd-loop run` exits 0.

## Why the agent can't fake it

- The receipt is produced by **running the code and reading an external store**,
  not by the agent's report. (`ship()` returning ≠ arrival — that's the whole
  "positive" idea, inherited from `ooptdd`.)
- **Longinus** rejects a GREEN that isn't backed by the claimed source: the symbol
  must exist and the event literal must be in its body, with a sha256 baseline so
  later drift is caught.
- The loop **refuses to let the agent edit the spec to pass** — the next-step
  context says so explicitly, and code review should treat spec edits as suspect.

## Honest limits (so this isn't itself a hallucination)

This makes wrong development **detectable and self-correcting**, not *impossible*.
It is exactly as good as your gates: a requirement with no gate proves nothing; a
gate that only checks existence won't catch a wrong value. `inconclusive`
(store unreachable) never fails the build — infrastructure outages must not mask
results, but they also mean "not verified, not refuted". Keep a human on the
critical path. Log-free zones from the methodology still apply (precise numerics,
security redaction, µs races — verify those another way).

## Driving an agent against it

```bash
# one pass; exit 0 = complete, 1 = work remains (RCA printed to stderr)
ooptdd-loop run example/requirements.yaml

# machine-readable for an orchestrator that feeds RCA back to the agent:
ooptdd-loop run spec.yaml --json
```

A minimal autonomous driver: run `--json`; if `complete` is false, hand the
`requirements[].checks` + the stderr RCA to the dev agent as its next prompt;
re-run after its edit; stop when `complete` is true. The loop is deterministic;
only the dev step is the model.

## Letting it drive itself: bounded, resumable, contained

`--fix` hands the loop the agent directly, and `--passes` alone is not a budget:
an agent can burn an hour of wall-clock or $40 inside a *single* pass, and a fix
that never returns makes the between-pass checks unreachable. `ooptdd_loop/harness.py`
is where every bound lives; the loop stops with a typed `loop_reason`:

| you set | it stops with | when |
|---|---|---|
| `--passes N` | `max_passes` / `single_pass` | the step ceiling |
| `--patience N` | `stalled` | N passes in a row changed nothing |
| `--max-seconds S` | `budget_time` | wall-clock spent (also bounds each fix) |
| `--max-spend X --spend-file F` | `budget_spend` | the meter reads ≥ X — **or cannot be read** |
| `--fix-timeout S` | `fix_timeout` | the fix is killed, with everything it spawned |
| `--fix-write-allow P` | `writeset_violation` | the fix wrote outside P — **or the audit couldn't run** |

Those last two "or" clauses are the rule, not an edge case: a budget we cannot
meter and an audit we cannot run **stop** the loop. An audit that cannot run and
passes is worse than no audit, because it reports safety.

```bash
# crash-safe: one JSONL line per pass, resume at the next unpaid pass
ooptdd-loop run spec.yaml --fix "$AGENT" --passes 20 \
  --journal .ooptdd/run.jsonl --run-id nightly-42 --max-seconds 1800 --fix-timeout 300
ooptdd-loop run spec.yaml --fix "$AGENT" --passes 20 \
  --journal .ooptdd/run.jsonl --run-id nightly-42 --resume     # ← after a crash
```

Resume replays the journal for `--run-id`, restores the pass counter and the
stall state, and re-measures the code on disk — it never repays the agent for a
pass already bought, and never reports a verdict it did not measure.

### The fix command's environment is scrubbed — a deliberate break

**This changed.** The fix command used to inherit the whole parent environment;
it now gets a minimal allowlist (`PATH`, `HOME`, `TMPDIR`, locale) plus the
loop's own `OOPTDD_*` variables, and nothing else. Handing an agent every
credential the loop happens to hold, for a job whose declared need is "edit
source from an RCA", is not a default worth keeping.

If your fix needs a credential, say so — or opt out of the scrub entirely:

```bash
ooptdd-loop run spec.yaml --fix "$AGENT" --fix-env-allow ANTHROPIC_API_KEY
ooptdd-loop run spec.yaml --fix "$AGENT" --fix-env-allow '*'   # pre-scrub behavior
```

### What `--fix-write-allow` does and does not promise

It audits what **git can see** in the target's work tree: the working tree, the
gitignored paths (`--ignored=matching`, so a write cannot hide in one), and
everything that differs from the pre-fix HEAD (so a fix that commits its own
writes cannot leave a clean `git status` and pass). It does **not** see writes
outside the work tree — `/tmp`, `$HOME`, another checkout, a network call. So
the honest claim is *"no write git can see landed outside the declared paths"*,
not *"the fix only wrote inside the declared paths"*. Real confinement of an
untrusted fix needs an OS sandbox; this is a tripwire.

One consequence: the audited write-set is deliberately over-inclusive, because
attributing a path to *this* fix is not something git can answer. Allowlist what
the run itself produces (`__pycache__/`, `.venv/`, `.ooptdd/`) alongside the
source the fix may edit.

## Harness surface map

- **Local coding harness (L_IDE)**: `ooptdd-loop run`, `validate-spec`, pytest,
  `next_step_context`.
- **Agent runtime harness (L_RT)**: `ooptdd-loop-mcp`, `ooptdd-loop mcp`, and
  `ooptdd_loop.tools.call()` expose the loop as callable tools. `logserver_*`
  tools bridge to the upstream `oo-mcp` log server for runtime evidence.
- **Managed/control-plane harness (L_MC)**: KG seed, coverage, drift, and
  Longinus ReferenceSites make completion and source drift queryable. RED RCA
  reads log-server MCP `trace_cycle` first, then falls back to the local backend.

Run `ooptdd-loop harness-profile --json` for the machine-readable map.
