import os

from ooptdd.backends import memory_reset
from ooptdd_loop.runner import run_loop
from ooptdd_loop.domain.spec import load_spec


ROOT = os.path.dirname(os.path.dirname(__file__))
EXAMPLE = os.path.join(ROOT, "example")


def test_logserver_pilot_target_completes_with_fake_health(tmp_path, monkeypatch):
    monkeypatch.setenv("OOPTDD_PILOT_FAKE_HEALTH", "1")
    memory_reset()
    spec = tmp_path / "pilot.yaml"
    spec.write_text(f"""
name: ooptdd-logserver-mcp-pilot-test
methodology:
  name: OOPTDD_methodology_v1
  enforce: true
target:
  mode: in_process
  callable: logserver_pilot:check_logserver_health
  backend: memory
  root: {EXAMPLE}
contracts:
  - id: MC-LOGSERVER-HEALTH
    kind: message_contract
    role: OOPTDDRuntime
    receiver: OoMcpLogServer
    message: logserver_health
    status: accepted
    source_req: REQ-LOGSERVER-HEALTH
    integration_backstop: REQ-LOGSERVER-HEALTH
requirements:
  - id: REQ-LOGSERVER-HEALTH
    kind: guiding
    description: fake health still exercises the event contract
    covers: [MC-LOGSERVER-HEALTH]
    gate:
      - {{event: logserver_health_checked, where: {{reachable: true}}, op: "==", count: 1}}
    longinus:
      kg_anchor: ref_site:ooptdd:logserver_mcp_pilot
      source: logserver_pilot.py
      symbol: check_logserver_health
      must_emit: logserver_health_checked
""")

    run = run_loop(load_spec(str(spec)))
    assert run.complete, [(r.id, r.checks, r.rca) for r in run.results]
    assert run.methodology_ok
    assert run.results[0].done
    memory_reset()
