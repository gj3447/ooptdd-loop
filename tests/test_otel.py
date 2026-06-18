import pytest


def test_otel_recorder_emits_session_test_and_requirement_spans():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from ooptdd_loop.otel import create_otel_recorder
    from ooptdd_loop.runner import ReqResult, RunResult

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("ooptdd-loop-test")
    traceparent = "00-1234567890abcdef1234567890abcdef-fedcba0987654321-01"

    recorder = create_otel_recorder(
        cid="cid-otel",
        backend="memory",
        spec_path="requirements.yaml",
        trace_parent=traceparent,
        worker=False,
        tracer=tracer,
    )
    run = RunResult(
        cid="cid-otel",
        backend="memory",
        results=[
            ReqResult(
                id="REQ-CHECKOUT",
                description="checkout emits a completion event",
                gate_ok=True,
                reachable=True,
                checks=[],
                binding=None,
            )
        ],
    )

    recorder.start_session()
    recorder.record_test_stage(
        nodeid="test_checkout.py::test_checkout_flow",
        stage="call",
        outcome="passed",
        duration=0.01,
    )
    recorder.record_run_result(run)
    recorder.finish_session(run)

    spans = exporter.get_finished_spans()
    by_name = {span.name: span for span in spans}
    assert {"ooptdd.pytest.session", "ooptdd.pytest.test", "ooptdd.requirement"} <= set(by_name)
    assert recorder.summary()["spans_created"] == 3

    trace_id = int("1234567890abcdef1234567890abcdef", 16)
    assert {span.context.trace_id for span in spans} == {trace_id}
    assert by_name["ooptdd.pytest.session"].attributes["ooptdd.cid"] == "cid-otel"
    assert by_name["ooptdd.pytest.test"].attributes["pytest.nodeid"].endswith(
        "test_checkout_flow"
    )
    assert by_name["ooptdd.requirement"].attributes["ooptdd.requirement.done"] is True
