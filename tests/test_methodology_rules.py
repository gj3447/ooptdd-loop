import json
import os

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop.kg_seed import seed_cypher, seed_payload
from ooptdd_loop.report import next_step_context
from ooptdd_loop.runner import run_loop
from ooptdd_loop.rules import canonical_rules
from ooptdd_loop.domain.spec import load_spec

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
EXAMPLE = os.path.join(ROOT, "example")


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _write_ooptdd_spec(tmp_path, *, backstop=True, private=False):
    integration_backstop = "REQ-PAY" if backstop else ""
    message = "_authorize_payment" if private else "authorize_payment"
    p = tmp_path / "ooptdd.yaml"
    p.write_text(
        f"""
name: ooptdd-valid
methodology:
  name: OOPTDD_methodology_v1
  enforce: true
target:
  mode: in_process
  callable: shop:run_pipeline
  backend: memory
  root: {EXAMPLE}
contracts:
  - id: MC-PAYMENT-AUTH
    kind: message_contract
    role: PaymentAuthorizer
    receiver: PaymentAuthorizer
    message: {message}
    status: accepted
    source_req: REQ-PAY
    integration_backstop: {integration_backstop}
requirements:
  - id: REQ-PAY
    kind: guiding
    description: payment is authorized exactly once
    covers: [MC-PAYMENT-AUTH]
    gate:
      - {{event: payment_authorized, op: "==", count: 1}}
    longinus:
      kg_anchor: ref_site:shop:payment
      source: shop.py
      symbol: authorize_payment
      must_emit: payment_authorized
"""
    )
    return str(p)


def test_ooptdd_methodology_rules_can_complete(tmp_path):
    run = run_loop(load_spec(_write_ooptdd_spec(tmp_path)))
    assert run.complete
    assert run.methodology_checks
    assert all(c.passed for c in run.methodology_checks)


def test_ooptdd_missing_integration_backstop_blocks_done(tmp_path):
    run = run_loop(load_spec(_write_ooptdd_spec(tmp_path, backstop=False)))
    assert not run.complete
    assert run.n_done == 1  # runtime gate and Longinus are green, methodology blocks
    failed = {c.rule_id: c.message for c in run.methodology_checks if not c.passed}
    assert "OOPTDD-R09" in failed
    assert "MC-PAYMENT-AUTH" in failed["OOPTDD-R09"]
    assert "OOPTDD-R09" in next_step_context(run)


def test_private_helper_contract_is_rejected(tmp_path):
    run = run_loop(load_spec(_write_ooptdd_spec(tmp_path, private=True)))
    failed = {c.rule_id for c in run.methodology_checks if not c.passed}
    assert "OOPTDD-R06" in failed
    assert not run.complete


def test_canonical_rules_and_kg_seed_are_stable():
    rules = canonical_rules()
    payload = seed_payload()
    cypher = seed_cypher()
    assert len(rules) == 14
    assert payload["method"]["name"] == "OOPTDD_methodology_v1"
    assert any(r["id"] == "OOPTDD-R09" for r in payload["rules"])
    assert "MERGE (m:AbstractNode:Methodology" in cypher
    json.dumps(payload)
