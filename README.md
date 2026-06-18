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

## Docs

- `README.md` is the quickstart and spec reference.
- `AGENT_LOOP.md` explains the edit-run-RCA loop.
- `docs/HARNESS_INTEGRATION.md` maps the package to common harness surfaces:
  CLI, pytest, MCP, KG, Longinus, and oo/OpenObserve.
- `docs/OSS_INTAKE.md` records the PROM/KG-driven open-source intake and
  adoption plan for pytest, OTel, trace gates, and golden traces.
- `example/*.yaml` files are executable specs.

## Verification

Run the local harness before widening OOPTDD behavior:

```bash
scripts/verify_ooptdd.sh
scripts/clean_install_smoke.sh
scripts/mcp_stdio_smoke.py --run
scripts/real_backend_xdist_otel_pilot.sh
```

It runs ruff, focused pytest runtime/OTel tests, the full pytest suite, CLI/MCP
metadata checks, MCP stdio roundtrip calls, memory-backed example specs, and
golden save/diff smoke checks.
Use `scripts/verify_ooptdd.sh --include-external` to include logserver and
OpenObserve-backed checks when the required environment variables are present.
`scripts/real_backend_xdist_otel_pilot.sh` is the focused external pilot for
pytest-xdist, `TRACEPARENT`, OpenObserve readback, and logserver MCP trace
lookup.

`scripts/clean_install_smoke.sh` creates a fresh virtualenv, installs
`ooptdd-loop` editable with `dev,mcp,kg,otel,xdist` extras, checks console
scripts, requires the MCP stdio smoke to pass, then runs the same local harness
from that environment. In this
workspace it auto-detects the sibling `ooptdd` checkout; use
`--no-local-ooptdd` to force package-index resolution or `--ooptdd-path` to
point at another dependency checkout.

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

## OOPTDD methodology mode

`methodology.enforce: true` turns on the OOPTDD rule engine. In that mode a
requirement is complete only when the LTDD trace gate is GREEN, the Longinus
binding is bound, and the object/message methodology checks pass:

```bash
ooptdd-loop run example/requirements_ooptdd.yaml
ooptdd-loop rules --cypher --params      # export idempotent KG seed
ooptdd-loop seed-kg                      # write through NEO4J_* env, if configured
ooptdd-loop validate-spec example/requirements_ooptdd.yaml
ooptdd-loop tools                        # list MCP/agent tool registry
ooptdd-loop harness-profile              # L_IDE/L_RT/L_MC map
ooptdd-loop mcp --check                  # inspect MCP server metadata
ooptdd-loop mcp-config --json            # generate Claude/Codex MCP config snippets
ooptdd-loop mcp-config --check --json    # verify local Claude/Codex MCP registration
ooptdd-loop-mcp                          # run MCP stdio server
ooptdd-loop logserver-health             # query upstream oo-mcp ingest health
```

The canonical rules include outside-in guiding tests, mock-as-contract-candidate,
domain-message-only filtering, integration backstops, Longinus ReferenceSites,
reverse orphan scan readiness, log-free zones, and the final done condition.

## Pytest runtime plugin

For pytest-based harnesses, pytest itself can produce the evidence and OOPTDD can
judge it at session finish:

```bash
pytest --ooptdd-spec example/requirements_pytest.yaml \
  --ooptdd-cid pytest-local-1 \
  --ooptdd-report .ooptdd/pytest-local-1.json
```

When `--ooptdd-spec` is present, the plugin sets `OOPTDD_CID`,
`OOPTDD_BACKEND`, and `OOPTDD_SPEC` for tests and xdist workers. With
`--ooptdd-trace-parent`, the same W3C `TRACEPARENT` is propagated into workers
and recorded in report metadata. If OpenTelemetry API is installed, the plugin
also emits `ooptdd.pytest.session`, `ooptdd.pytest.test`, and
`ooptdd.requirement` spans through the active OTel tracer provider; if OTel is
not installed, the same pytest path stays no-op and dependency-free. Tests ship
events under that cid; after pytest finishes, OOPTDD evaluates the configured
trace gates and Longinus bindings without recursively running pytest again. For
the offline memory backend, xdist workers forward serializable event evidence to
the controller before the final verdict, so `pytest -n auto --ooptdd-spec ...`
remains deterministic. A RED requirement changes the pytest session exit code to
`1`, while the JSON receipt records test stages, OTel summary, xdist forwarding
counts, trace context, and the OOPTDD verdict.

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

## Selector gates

For trace-style assertions, `select` filters by event plus structured fields such
as `service`, `operation`, `where`, `attrs`, or `attributes`:

```yaml
gate:
  - select:
      event: payment_authorized
      service: billing
      operation: authorize
      attrs: {amount: 42}
    op: "=="
    count: 1
```

Selectors also work in order and causal checks:

```yaml
gate:
  - must_order:
      - {event: order_received, service: web}
      - {event: payment_authorized, service: billing, operation: authorize}
      - {event: order_shipped, service: fulfillment}
  - select: {event: payment_authorized, service: billing}
    after: {event: order_received, service: web}
    within_s: 1
```

`example/requirements_selectors.yaml` is a runnable selector spec.

## Local structured capture

For in-process Python targets that already emit structured logs, OOPTDD can
capture those records into the configured backend during the run:

```yaml
target:
  mode: in_process
  callable: logging_app:run_pipeline
  backend: memory
  root: example
  capture:
    logging: true
    logger: checkout
```

Supported shapes are `logging` records with `extra={"event": ...}` fields and
dict log messages such as `logger.info({"event": "order_shipped", ...})`. The
adapter adds `cid`, `correlation_id`, and `cycle_id`, then normalizes the record
into the same event envelope used by other OOPTDD backends.

`example/requirements_local_capture.yaml` is a runnable local-capture spec.

## Golden traces

After a spec is GREEN, save the accepted event shape as a deterministic baseline:

```bash
ooptdd-loop golden save example/requirements_selectors.yaml \
  --run \
  --cid golden-selectors-v1 \
  --out .ooptdd/golden/selectors.json
```

Later runs can diff against that baseline:

```bash
ooptdd-loop golden diff example/requirements_selectors.yaml \
  .ooptdd/golden/selectors.json \
  --run \
  --cid golden-selectors-next
```

The diff status is one of `PASSED`, `TOOLS_CHANGED`, `OUTPUT_CHANGED`, or
`REGRESSION`. By default CLI diff fails only on `REGRESSION`; use `--strict` to
fail on any non-`PASSED` status.

## Property fuzzing

The dev test extra includes Hypothesis for evaluator hardening. The property
tests cover selector cardinality, first-occurrence ordering with duplicates and
missing events, and golden diff status priority:

```bash
pytest tests/test_property_fuzzing.py
```

## Real backend (oo-mcp)

Offline, everything uses the in-memory backend (the example + this repo's tests
need no infrastructure). For real cross-process verification, point the target at
OpenObserve. The loop polls the store for gates and, on RED, reads the
log-server MCP endpoint first (`OO_MCP_URL`, defaulting to the workspace
`oo-mcp`) for the cross-stream `trace_cycle` RCA. The `oo` CLI remains a fallback
client for the same MCP server.

```yaml
target:
  mode: command
  command: "pytest -q"          # your suite ships events to oo under OOPTDD_CID
  backend: openobserve
```

```bash
export OOPTDD_OO_URL=…  OOPTDD_OO_PASSWORD=…   # secrets: env only
export OO_MCP_URL=http://host:55014/mcp         # optional; default is workspace oo-mcp
ooptdd-loop run spec.yaml
ooptdd-loop logserver-health
ooptdd-loop logserver-trace "$OOPTDD_CID"
```

The OOPTDD MCP server exposes the same log-server bridge as agent tools:
`logserver_tools`, `logserver_health`, `logserver_trace`, `logserver_query`, and
`logserver_errors`. That lets an agent connect only to `ooptdd-loop-mcp` and
still retrieve OpenObserve evidence through the upstream log MCP server.

### Logserver MCP pilot

`example/requirements_logserver_mcp.yaml` is the first real integration pilot.
It calls upstream `oo-mcp` health, ships `logserver_health_checked` to the
`ooptdd_demo` OpenObserve stream, and requires OOPTDD to read that event back:

```bash
ooptdd-loop run example/requirements_logserver_mcp.yaml --cid pilot-logserver-mcp-20260616
ooptdd-loop logserver-trace pilot-logserver-mcp-20260616 --minutes-back 30
```

## Harness integrations

The package exposes the same core loop through the surfaces most agent harnesses
need:

- **CLI**: `ooptdd-loop run`, `validate-spec`, `coverage`, `drift`, `tools`,
  `mcp-config`, `golden save`, `golden diff`, `logserver-health`,
  `logserver-trace`, `logserver-query`, `logserver-errors`.
- **pytest/CI**: `pytest --ooptdd-spec` evaluates trace gates at session finish;
  memory backend examples and tests run offline, including xdist worker evidence
  forwarding, `TRACEPARENT` propagation, and optional OTel span emission.
- **MCP/runtime tools**: `ooptdd-loop-mcp` and `ooptdd-loop mcp` expose
  `ooptdd_loop.tools.TOOLS`.
- **KG/Longinus**: `seed-kg`, `kg_seed`, `coverage`, `drift`, ReferenceSites.
- **oo/OpenObserve**: OpenObserve backend plus MCP-first logserver tools and
  `oo trace` fallback.

## Relation to the rest of the workspace

- **`ooptdd`** (the library) provides the verify/backend/gate primitives this loop
  orchestrates.
- **Longinus** bindings here mirror the 7-tuple ReferenceSite shape of the full
  drift engine in `../bhgman_tool/engine/longinus_drift_audit/`; `--kg-write`
  persists them to the KG when `NEO4J_*` env is set.
- The methodology is `ooptdd`'s `METHODOLOGY.md` (LTDD).

## Status

`0.1.0`. Tests + example run fully offline (memory backend). Apache-2.0.
