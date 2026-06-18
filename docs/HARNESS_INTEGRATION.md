# OOPTDD Harness Integration

KG: OOPTDD_methodology_v1

OOPTDD is wired as a harness component across the three common harness layers.
The same verdict model is exposed through CLI, tests, MCP tools, KG, and
Longinus bindings.

## L_IDE: local coding harness

Use this when Claude Code, Codex, Aider, Cursor, or another local coding harness
is editing the repository.

- `ooptdd-loop run <spec.yaml>` executes the system and returns the verdict.
- `ooptdd-loop run <spec.yaml> --json` returns machine-readable agent context.
- `pytest --ooptdd-spec <spec.yaml>` lets pytest produce the evidence and uses
  OOPTDD as a session-finish gate.
- xdist workers inherit `OOPTDD_CID`, `OOPTDD_BACKEND`, `OOPTDD_SPEC`, and
  optional `TRACEPARENT`; memory-backend worker evidence is forwarded to the
  controller before final evaluation.
- `target.capture.logging` captures local structured Python logs into the same
  backend envelope as normal OOPTDD events.
- `ooptdd-loop validate-spec <spec.yaml>` checks OOPTDD methodology rules without
  executing code.
- `pytest` remains the fast local verification path.
- `next_step_context` is the correction payload: it tells the agent what is RED
  without guessing.

The L_IDE axes are:

- Inform: `list_requirements`, `methodology_rules`
- Constrain: `validate_spec`, OOPTDD methodology checks
- Verify: `run`, `verify`, `ontology_lookup`, `golden_diff`,
  `logserver_trace`, `logserver_query`, selector gates over
  event/service/operation/attrs/order
- Correct: `rca`, `next_step`, `logserver_errors`, `golden_save`,
  local structured capture

## L_RT: agent runtime harness

Use this when OOPTDD is called from a runtime agent framework or a multi-agent
orchestrator.

- `ooptdd_loop.tools` is a pure Python registry. It has no MCP dependency.
- `ooptdd-loop-mcp` exposes the same registry through MCP stdio.
- `pytest11:ooptdd_loop` exposes the runtime gate to pytest/CI harnesses without
  a separate wrapper script.
- `--ooptdd-trace-parent` provides OTel-friendly W3C trace context propagation.
  When OpenTelemetry API is installed, the plugin emits session, test-stage, and
  requirement verdict spans through the active tracer provider without making
  OTel a hard dependency.
- `logserver_*` tools bridge to the upstream `oo-mcp` log-server endpoint through
  `OO_MCP_URL`, so agents can query OpenObserve evidence without separate
  credentials.
- `ooptdd-loop mcp --check` verifies the MCP entrypoint without starting a
  long-running stdio server.
- `ooptdd-loop mcp-config` generates Claude/Codex registration fragments and
  `ooptdd-loop mcp-config --check` verifies local registration drift.
- `scripts/mcp_stdio_smoke.py --run` starts the stdio MCP server, verifies every
  registry tool is exposed, and calls representative tools through a real MCP
  client session.
- `ooptdd-loop tools --json` lists the available tool schemas.
- `golden_save` and `golden_diff` expose deterministic baseline save/diff to MCP
  and direct Python harnesses.

Current tool registry:

- `list_requirements`
- `run`
- `validate_spec`
- `methodology_rules`
- `kg_seed`
- `harness_profile`
- `verify`
- `rca`
- `logserver_tools`
- `logserver_health`
- `logserver_trace`
- `logserver_query`
- `logserver_errors`
- `golden_save`
- `golden_diff`
- `ontology_lookup`
- `coverage`
- `drift`

## L_MC: managed/control-plane harness

Use this when OOPTDD evidence must be queryable after a run.

- `ooptdd-loop seed-kg` writes OOPTDD rule ontology into Neo4j when `NEO4J_*` is
  configured.
- `ooptdd-loop rules --cypher --params` exports the same idempotent seed.
- `coverage(spec_name)` answers which requirements are DONE from KG state.
- `drift(spec_name)` answers which Longinus bindings changed sha256.
- `ReferenceSite` nodes bind the OOPTDD methodology and engine back to source.
- `golden_save` records accepted event shape; `golden_diff` classifies later
  runs as `PASSED`, `TOOLS_CHANGED`, `OUTPUT_CHANGED`, or `REGRESSION`.
- Local structured capture lets offline in-process tests use logging/structlog
  style events without a networked store.
- xdist hardening keeps offline memory-backend pytest runs deterministic by
  replaying worker event fragments in the controller process.
- OTel span emission records pytest/OOPTDD feedback as `ooptdd.pytest.session`,
  `ooptdd.pytest.test`, and `ooptdd.requirement` spans when an OTel provider is
  available.
- Hypothesis property tests harden selector/golden evaluator invariants in the
  dev harness.
- `scripts/verify_ooptdd.sh` is the single local verification entrypoint for
  ruff, pytest, CLI/MCP metadata, MCP stdio roundtrip, Claude/Codex MCP config
  generation, memory examples, and golden smoke checks.
- `scripts/clean_install_smoke.sh` verifies packaging, editable install extras,
  console scripts, MCP stdio behavior, pytest entrypoint behavior, and the same
  local harness inside a fresh virtualenv.
- `scripts/real_backend_xdist_otel_pilot.sh` verifies pytest-xdist workers,
  `TRACEPARENT`, OpenObserve arrival, and logserver MCP trace lookup against the
  real backend when external credentials are configured.
- `logserver_health`, `logserver_trace`, `logserver_query`, and
  `logserver_errors` read the MCP-backed OpenObserve log server.

## Minimum complete integration

A harness integration is considered complete when all of these work:

1. The agent can discover requirements through `list_requirements` or CLI.
2. The agent can run the loop and receive `next_step` on failure.
3. Pytest/CI can run with `--ooptdd-spec` and fail the session on RED gates.
4. The agent can validate OOPTDD rules before implementation.
5. The runtime can call the same surface through MCP or direct Python.
6. KG can store OOPTDD rules and Longinus ReferenceSites.
7. Coverage and drift can be queried without re-running the target.
8. Golden baselines can be saved and compared through CLI or MCP tools.
9. The runtime can query log-server MCP health and trace evidence through
   OOPTDD's MCP tool surface.
10. xdist workers can preserve offline memory evidence and shared trace context.

## Commands

```bash
scripts/verify_ooptdd.sh
scripts/clean_install_smoke.sh
scripts/mcp_stdio_smoke.py --run
scripts/real_backend_xdist_otel_pilot.sh
ooptdd-loop run example/requirements_ooptdd.yaml
ooptdd-loop run example/requirements_selectors.yaml
ooptdd-loop run example/requirements_local_capture.yaml
pytest --ooptdd-spec example/requirements_ooptdd.yaml --ooptdd-report .ooptdd/pytest.json
ooptdd-loop golden save example/requirements_selectors.yaml --run --out .ooptdd/golden/selectors.json
ooptdd-loop golden diff example/requirements_selectors.yaml .ooptdd/golden/selectors.json --run
ooptdd-loop validate-spec example/requirements_ooptdd.yaml
ooptdd-loop tools --json
ooptdd-loop harness-profile --json
ooptdd-loop mcp --check
ooptdd-loop mcp-config --json
ooptdd-loop mcp-config --check --json
ooptdd-loop logserver-health
ooptdd-loop logserver-trace "$OOPTDD_CID"
ooptdd-loop rules --cypher --params
ooptdd-loop seed-kg
ooptdd-loop-mcp
```
