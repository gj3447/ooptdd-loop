# ooptdd-loop

**Develop until the logs prove it.** An agent-driven, positive-TDD requirements
loop built on [`ooptdd`](https://github.com/airobotics-ailab/ooptdd) — designed to
catch AI hallucination by construction.

You declare requirements as **trace gates** (the events the system must emit) plus
a **Longinus binding** (the source symbol that must emit them). An agent writes
code; the loop runs it, waits until those events actually **arrive** in the store
(via the `ooptdd` backend / `oo-mcp`), and only then marks the requirement done —
and only if the binding points at real, emitting source. Anything RED comes back
with a **log-grounded RCA**, so the agent fixes from evidence, not guesses. Repeat
until every requirement is GREEN and bound.

> The honest claim: this makes wrong development **detectable and
> self-correcting**, not magically impossible. See `AGENT_LOOP.md` for the loop
> diagram and the limits.

## Quick look

```bash
pip install -e .          # depends on the ooptdd package
ooptdd-loop run example/requirements.yaml
```

```
ooptdd-loop  cid=loop-…  backend=memory
requirements: 4/4 DONE  -> COMPLETE ✅

✅ REQ-1       gate=GREEN         longinus=bound    an order is received exactly once
✅ REQ-2       gate=GREEN         longinus=bound    payment is authorized exactly once
✅ REQ-3       gate=GREEN         longinus=bound    at least 3 line items are packed
✅ REQ-4       gate=GREEN         longinus=bound    the order is shipped exactly once
```

Now see what the agent sees mid-loop — a RED gate and an UNBOUND binding:

```bash
ooptdd-loop run example/requirements_unmet.yaml ; echo "exit=$?"
```

```
❌ REQ-FRAUD   gate=RED           longinus=UNBOUND  a fraud check runs before payment
     gate miss: fraud_checked == 1 (got 0)
     longinus: symbol 'run_fraud_check' not defined in shop.py
✅ REQ-1       gate=GREEN         longinus=bound    an order is received exactly once
exit=1
# stderr: log-grounded RCA + "fix the code; do not edit the spec to pass"
```

## How a requirement is judged

A requirement is **DONE** iff:

1. **gate GREEN** — every expected event actually arrived in the store for this
   run's correlation id (positive arrival, polled; `inconclusive` if the store is
   unreachable — which never fails the build), and
2. **Longinus bound** — the bound symbol exists in the named source and its body
   emits the event literal (sha256 baseline captured for drift).

The verdict is produced by running the code and reading an external store the
agent can't fake — that's why it catches hallucinated "done".

## Spec format

```yaml
target:
  mode: in_process              # in_process: call module:fn(backend, cid)
  callable: shop:run_pipeline   # (or)  mode: command + command: "pytest -q"
  backend: memory               # memory | openobserve | otel | <entrypoint>
  root: example                 # source root for Longinus + import

requirements:
  - id: REQ-2
    description: payment is authorized exactly once
    gate:
      - {event: payment_authorized, op: "==", count: 1}
    longinus:
      kg_anchor: ref_site:shop:payment
      source: shop.py
      symbol: authorize_payment
      must_emit: payment_authorized
```

## Real backend (oo-mcp)

Offline, everything uses the in-memory backend (the example + this repo's tests
need no infrastructure). For real cross-process verification, point the target at
OpenObserve — the loop then polls the store and, on RED, pulls
`oo trace <cid>` for the cross-stream timeline RCA:

```yaml
target:
  mode: command
  command: "pytest -q"          # your suite ships events to oo under OOPTDD_CID
  backend: openobserve
```

```bash
export OOPTDD_OO_URL=…  OOPTDD_OO_PASSWORD=…   # secrets: env only
ooptdd-loop run spec.yaml
```

## Relation to the rest of the workspace

- **`ooptdd`** (the library) provides the verify/backend/gate primitives this loop
  orchestrates.
- **Longinus** bindings here mirror the 7-tuple ReferenceSite shape of the full
  drift engine in `../bhgman_tool/engine/longinus_drift_audit/`; `--kg-write`
  persists them to the KG when `NEO4J_*` env is set.
- The methodology is `ooptdd`'s `METHODOLOGY.md` (LTDD).

## Status

`0.1.0`. Tests + example run fully offline (memory backend). Apache-2.0.
