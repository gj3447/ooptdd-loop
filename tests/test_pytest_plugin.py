import json

import pytest

pytest_plugins = ["pytester"]
OOPTDD_PLUGIN_ARGS = ("-p", "no:ooptdd_loop", "-p", "ooptdd_loop.pytest_plugin")


def _write_app_and_spec(pytester, *, emit: bool) -> tuple[str, str]:
    pytester.makepyfile(
        app_under_test="""
        import os
        from ooptdd.backends import get_backend


        def emit_checkout_complete():
            backend = get_backend(os.environ["OOPTDD_BACKEND"])
            cid = os.environ["OOPTDD_CID"]
            backend.ship([
                {
                    "cid": cid,
                    "correlation_id": cid,
                    "cycle_id": cid,
                    "service": "checkout",
                    "event": "checkout_completed",
                }
            ])
        """
    )
    body = "emit_checkout_complete()" if emit else "pass"
    pytester.makepyfile(
        test_checkout=f"""
        from app_under_test import emit_checkout_complete


        def test_checkout_flow():
            {body}
        """
    )
    spec = pytester.path / "requirements.yaml"
    spec.write_text(
        f"""
target:
  mode: pytest
  backend: memory
  root: {pytester.path}
requirements:
  - id: REQ-CHECKOUT
    description: checkout emits a completion event
    gate:
      - {{event: checkout_completed, op: "==", count: 1}}
    longinus:
      kg_anchor: ref_site:pytest:checkout
      source: app_under_test.py
      symbol: emit_checkout_complete
      must_emit: checkout_completed
""",
        encoding="utf-8",
    )
    report = pytester.path / "ooptdd-report.json"
    return str(spec), str(report)


def test_pytest_plugin_marks_session_green_when_spec_is_done(pytester):
    spec, report = _write_app_and_spec(pytester, emit=True)

    result = pytester.runpytest(
        *OOPTDD_PLUGIN_ARGS,
        "--ooptdd-spec",
        spec,
        "--ooptdd-cid",
        "pytest-plugin-green",
        "--ooptdd-report",
        report,
    )

    result.assert_outcomes(passed=1)
    assert result.ret == 0
    payload = json.loads((pytester.path / "ooptdd-report.json").read_text())
    assert payload["cid"] == "pytest-plugin-green"
    assert payload["complete"] is True
    assert payload["done"] == payload["total"] == 1
    assert payload["evaluation_attempts"] == 1
    assert payload["otel"]["spans_created"] >= 0
    assert payload["requirements"][0]["done"] is True
    assert payload["tests"]["test_checkout.py::test_checkout_flow"]["call"]["outcome"] == "passed"


def test_pytest_plugin_fails_session_when_trace_gate_is_red(pytester):
    spec, report = _write_app_and_spec(pytester, emit=False)

    result = pytester.runpytest(
        *OOPTDD_PLUGIN_ARGS,
        "--ooptdd-spec",
        spec,
        "--ooptdd-cid",
        "pytest-plugin-red",
        "--ooptdd-report",
        report,
    )

    result.assert_outcomes(passed=1)
    assert result.ret == 1
    payload = json.loads((pytester.path / "ooptdd-report.json").read_text())
    assert payload["complete"] is False
    assert payload["requirements"][0]["id"] == "REQ-CHECKOUT"
    assert payload["requirements"][0]["gate_ok"] is False


def test_pytest_plugin_runs_when_xdist_plugin_is_disabled(pytester):
    spec, report = _write_app_and_spec(pytester, emit=True)

    result = pytester.runpytest_subprocess(
        "-p",
        "no:xdist",
        *OOPTDD_PLUGIN_ARGS,
        "--ooptdd-spec",
        spec,
        "--ooptdd-cid",
        "pytest-plugin-no-xdist",
        "--ooptdd-report",
        report,
    )

    result.assert_outcomes(passed=1)
    assert result.ret == 0
    payload = json.loads((pytester.path / "ooptdd-report.json").read_text())
    assert payload["complete"] is True
    assert payload["xdist"]["forwarded_events"] == 0


def test_worker_env_payload_is_serializable():
    from ooptdd_loop.pytest_plugin import worker_input_payload

    payload = worker_input_payload(
        cid="cid-1",
        backend="memory",
        backend_options={"stream": "pilot"},
        spec="/tmp/spec.yaml",
        trace_parent="00-1234567890abcdef1234567890abcdef-fedcba0987654321-01",
    )

    json.dumps(payload)
    assert payload["OOPTDD_CID"] == "cid-1"
    assert payload["OOPTDD_BACKEND"] == "memory"
    assert json.loads(payload["OOPTDD_BACKEND_OPTIONS"]) == {"stream": "pilot"}
    assert payload["OOPTDD_SPEC"] == "/tmp/spec.yaml"
    assert payload["TRACEPARENT"].startswith("00-")


def test_pytest_plugin_xdist_forwards_memory_events_and_traceparent(pytester):
    pytest.importorskip("xdist")
    traceparent = "00-1234567890abcdef1234567890abcdef-fedcba0987654321-01"
    pytester.makepyfile(
        app_under_test="""
        import os
        from ooptdd.backends import get_backend


        def _ship(event):
            cid = os.environ["OOPTDD_CID"]
            get_backend(os.environ["OOPTDD_BACKEND"]).ship([
                {
                    "cid": cid,
                    "correlation_id": cid,
                    "cycle_id": cid,
                    "service": "checkout",
                    "event": event,
                    "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                }
            ])


        def emit_worker_one():
            _ship("worker_one_done")


        def emit_worker_two():
            _ship("worker_two_done")
        """
    )
    pytester.makepyfile(
        test_worker_one=f"""
        import os
        from app_under_test import emit_worker_one


        def test_worker_one():
            assert os.environ["TRACEPARENT"] == {traceparent!r}
            emit_worker_one()
        """,
        test_worker_two=f"""
        import os
        from app_under_test import emit_worker_two


        def test_worker_two():
            assert os.environ["TRACEPARENT"] == {traceparent!r}
            emit_worker_two()
        """,
    )
    spec = pytester.path / "requirements.yaml"
    spec.write_text(
        f"""
target:
  mode: pytest
  backend: memory
  root: {pytester.path}
requirements:
  - id: REQ-WORKER-ONE
    description: first worker event reaches controller verdict
    gate:
      - {{event: worker_one_done, op: "==", count: 1}}
    longinus:
      kg_anchor: ref_site:pytest:worker_one
      source: app_under_test.py
      symbol: emit_worker_one
      must_emit: worker_one_done
  - id: REQ-WORKER-TWO
    description: second worker event reaches controller verdict
    gate:
      - {{event: worker_two_done, op: "==", count: 1}}
    longinus:
      kg_anchor: ref_site:pytest:worker_two
      source: app_under_test.py
      symbol: emit_worker_two
      must_emit: worker_two_done
""",
        encoding="utf-8",
    )
    report = pytester.path / "ooptdd-report.json"

    result = pytester.runpytest_subprocess(
        "-p",
        "xdist",
        *OOPTDD_PLUGIN_ARGS,
        "-n",
        "2",
        "--ooptdd-spec",
        str(spec),
        "--ooptdd-cid",
        "pytest-plugin-xdist",
        "--ooptdd-trace-parent",
        traceparent,
        "--ooptdd-report",
        str(report),
    )

    result.assert_outcomes(passed=2)
    assert result.ret == 0
    payload = json.loads(report.read_text())
    assert payload["trace_parent"] == traceparent
    assert payload["xdist"]["forwarded_events"] == 2
    assert payload["complete"] is True
    assert payload["done"] == payload["total"] == 2
