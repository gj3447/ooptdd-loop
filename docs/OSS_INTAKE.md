# OOPTDD OSS Intake

Date: 2026-06-16

This intake turns the PROM/KG prior-art pass into concrete source checkouts and
adoption slots for `ooptdd-loop`. The goal is pattern absorption, not vendoring:
copying code from these projects requires a separate license review.

## Source Basis

- KG cycles: `prom16-agent-log-tdd-20260610`,
  `prom16-oo-ai-devloop-20260610`.
- PROM artifacts:
  - `BPC/PROM16_agent_log_tdd_20260610/SOURCES.md`
  - `BPC/PROM16_agent_log_tdd_20260610/_findings/ooptdd_A2_prior_art_landscape.md`
  - `BPC/PROM16_agent_log_tdd_20260610/_findings/ooptdd_B2_packaging_priorart.md`
  - `BPC/PROM16_agent_log_tdd_20260610/_findings/ooptdd_D2_competition_priorart.md`
- Local clone root: `../../external/ooptdd_oss/`

## Cloned Sources

| Project | Local path | Commit | Upstream | License posture |
| --- | --- | --- | --- | --- |
| `pytest-json-report` | `../../external/ooptdd_oss/pytest-json-report` | `c6f0bc93a174` | https://github.com/numirias/pytest-json-report.git | MIT |
| `pytest-xdist` | `../../external/ooptdd_oss/pytest-xdist` | `ed944469ff15` | https://github.com/pytest-dev/pytest-xdist.git | MIT |
| `pytest-opentelemetry` | `../../external/ooptdd_oss/pytest-opentelemetry` | `109224aff389` | https://github.com/chrisguidry/pytest-opentelemetry.git | MIT |
| `eval-view` | `../../external/ooptdd_oss/eval-view` | `99e1e8472f65` | https://github.com/hidai25/eval-view.git | Apache-2.0 |
| `tracetest` | `../../external/ooptdd_oss/tracetest` | `64eb49ff2037` | https://github.com/kubeshop/tracetest.git | Mixed: MIT plus Tracetest Community License by file |
| `structlog` | `../../external/ooptdd_oss/structlog` | `025edaf799c6` | https://github.com/hynek/structlog.git | MIT OR Apache-2.0 |
| `hypothesis` | `../../external/ooptdd_oss/hypothesis` | `8497a825e62e` | https://github.com/HypothesisWorks/hypothesis.git | MPL-2.0 |
| `malabi` | `../../external/ooptdd_oss/malabi` | `fdcff31dd186` | https://github.com/aspecto-io/malabi.git | Apache-2.0 |

## Adoption Slots

### P0: pytest Runtime Harness

Status: first implementation plus xdist/trace-context/span hardening landed in
`ooptdd_loop.pytest_plugin` and `ooptdd_loop.otel`.
Real-backend pytest/xdist pilot coverage lives in
`scripts/real_backend_xdist_otel_pilot.sh` with
`example/requirements_pytest_logserver_xdist.yaml`.

`pytest-json-report` is the cleanest model for a native OOPTDD pytest plugin. It
shows the hook shape needed to collect setup/call/teardown evidence, attach
metadata to reports, survive xdist serialization, and emit a session-level JSON
summary. For OOPTDD this maps to:

- `pytest_addoption`: `--ooptdd-spec`, `--ooptdd-cid`, `--ooptdd-report`,
  `--ooptdd-trace-parent`.
- `pytest_runtest_makereport`: attach requirement id, message contract id,
  trace gate outcome, and Longinus binding metadata to the report.
- `pytest_runtest_logreport`: aggregate per-test gate state by node id.
- `pytest_sessionfinish`: ship exactly one run receipt and fail only on RED
  requirements, not on unreachable observability stores.

`pytest-xdist` defines the controller/worker boundary. Workers collect and run
tests; the controller receives reports and forwards them through normal pytest
hooks. OOPTDD should therefore ship final run receipts from the controller only,
while workers only attach serializable evidence fragments to reports. The memory
backend now uses that pattern directly: workers collect events for the shared
cid, send JSON-safe fragments through `workeroutput`, and the controller replays
them before the final OOPTDD verdict.

`pytest-opentelemetry` gives the trace-parent and span hierarchy model. OOPTDD
should accept an incoming W3C trace parent, create a run span, and put test,
fixture, requirement, and gate data on child spans. For xdist, propagate trace
context through `workerinput` so worker events stay under the same run. The
current implementation keeps OTel optional: `--ooptdd-trace-parent` propagates
`TRACEPARENT` into tests/workers and records it in pytest user properties plus
the JSON receipt. When OpenTelemetry API is installed, OOPTDD emits
`ooptdd.pytest.session`, `ooptdd.pytest.test`, and `ooptdd.requirement` spans
through the active tracer provider; without OTel, the recorder degrades to a
no-op summary.

### P1: Trace Gates And Regression Baselines

Status: selector DSL first pass landed in `ooptdd_loop.selector_gates`; golden
trace first pass landed in `ooptdd_loop.golden`; local structured capture first
pass landed in `ooptdd_loop.local_capture`; Hypothesis-based property fuzzing
landed in `tests/test_property_fuzzing.py`.

`eval-view` is useful for golden trace behavior. OOPTDD already has positive
arrival gates; the next step is a deterministic baseline layer:

- `ooptdd-loop golden save <cid>` records accepted event shape, order policy,
  and tool/message sequence.
- `ooptdd-loop golden diff <cid>` reports `PASSED`, `TOOLS_CHANGED`,
  `OUTPUT_CHANGED`, or `REGRESSION`.
- CI can choose strictness independently from the normal gate verdict.

`tracetest` is useful as prior art for span selectors and attribute assertions.
OOPTDD should keep its YAML-first contract, but borrow the idea of selectors over
spans/events so gates can target service, operation, status, attributes, and
causal position instead of event name only.

`structlog` provides the right shape for local structured-log capture. It is the
offline complement to OpenObserve: use capture-style helpers for in-process
tests, then normalize captured event dictionaries into the same event envelope
used by external log backends.

### P2: Robustness And Cross-Ecosystem Ideas

`hypothesis` is not a runtime dependency target. It is a test-generation tool
for OOPTDD itself: fuzz gate operators, polling windows, duplicate events,
ordering policies, and malformed specs.

`malabi` is mostly useful conceptually. Its black-box OpenTelemetry test style is
close to OOPTDD's external-evidence stance, but the Node ecosystem fit is weaker
than pytest/OTel/OpenObserve for the current engine.

## Concrete Next Work

1. Add `pytest-ooptdd` plugin surface inside this package.
2. Make it xdist-safe by allowing only the controller to ship run receipts.
3. Add OTel trace context support without making OTel a hard dependency.
4. Add a selector DSL for trace gates: event, service, operation, attributes,
   count, order, and causal predecessor.
5. Add golden trace save/diff commands after the selector model is stable.
6. Use Hypothesis for evaluator-level property tests before widening the gate
   language.

Items 1-3 are implemented through
`pytest11:ooptdd_loop`, `--ooptdd-spec`, `--ooptdd-cid`, `--ooptdd-report`, and
worker-input propagation of `OOPTDD_CID`, `OOPTDD_BACKEND`, `OOPTDD_SPEC`, and
`TRACEPARENT`. The xdist hardening path also forwards memory-backend worker
events to the controller and records forwarding counts in the JSON receipt.
OpenTelemetry span emission is implemented in `ooptdd_loop.otel`.
The real backend pilot runs pytest under xdist, propagates `TRACEPARENT`, emits
events from workers into OpenObserve, evaluates arrival through OOPTDD, then
queries the logserver MCP trace for the same cid.

The MCP stdio runtime is covered by `scripts/mcp_stdio_smoke.py`, which starts
`ooptdd_loop.mcp_server` through the MCP SDK, verifies every registry tool is
exposed through stdio, and calls
`methodology_rules`, `validate_spec`, `harness_profile`, `list_requirements`,
and optionally `run`. `scripts/clean_install_smoke.sh` requires this path after
installing the `mcp` extra.

Claude/Codex registration drift is covered by `ooptdd-loop mcp-config`. It
generates reviewable Codex TOML and Claude `settings.json` fragments, and its
`--check` mode validates the local client config against the generated
`ooptdd-loop` stdio server entry.

Item 4 is implemented as the first P1 pass through `select`, selector
`must_order`, and `after` causal predecessor gates. The implementation lives in
`ooptdd_loop.selector_gates` and deliberately delegates non-selector gates to the
upstream `ooptdd.gate` evaluator.

Item 5 is implemented as the second P1 pass through `ooptdd-loop golden
save|diff` plus MCP tools `golden_save` and `golden_diff`. Baselines store
normalized event identity, stable payload shape, and requirement verdicts. Diff
status is deterministic: `PASSED`, `TOOLS_CHANGED`, `OUTPUT_CHANGED`, or
`REGRESSION`.

The local structured capture slot is implemented through `target.capture.logging`
and `ooptdd_loop.local_capture`. It supports Python logging records with
`extra={"event": ...}` fields, dict log messages, and a structlog-compatible
processor factory without adding structlog as a hard dependency.

The property-fuzzing slot is implemented as a dev-only test dependency on
Hypothesis. It currently fuzzes selector cardinality, first-occurrence ordering
under duplicates/missing events, and golden status priority. Hypothesis source is
not vendored.

## Non-Goals

- Do not vendor `tracetest`, `hypothesis`, or any mixed/MPL source into
  `ooptdd-loop`.
- Do not make OOPTDD a clone of Tracetest. OOPTDD remains agent-loop first:
  RED drives RCA and code repair through MCP/KG/Longinus.
- Do not require OpenTelemetry for the offline path. Memory and local structured
  capture must remain first-class for fast RED/GREEN loops.
