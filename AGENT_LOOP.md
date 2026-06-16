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
