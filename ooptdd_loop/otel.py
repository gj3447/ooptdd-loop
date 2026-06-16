"""Optional OpenTelemetry span emission for OOPTDD runtime integrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OtelSummary:
    available: bool
    enabled: bool
    spans_created: int = 0
    error: str | None = None


class NullOtelRecorder:
    def __init__(self, *, error: str | None = None) -> None:
        self._summary = OtelSummary(
            available=False,
            enabled=False,
            error=error,
        )

    def start_session(self) -> None:
        return None

    def record_test_stage(
        self,
        *,
        nodeid: str,
        stage: str,
        outcome: str,
        duration: float,
    ) -> None:
        return None

    def record_run_result(self, run_result) -> None:
        return None

    def finish_session(self, run_result=None, *, error: str | None = None) -> None:
        if error and not self._summary.error:
            self._summary.error = error

    def summary(self) -> dict[str, Any]:
        return self._summary.__dict__.copy()


class OtelRecorder:
    def __init__(
        self,
        *,
        cid: str,
        backend: str,
        spec_path: str,
        trace_parent: str | None,
        worker: bool,
        tracer,
        propagate_api,
        status_cls,
        status_code_cls,
        span_kind_cls,
    ) -> None:
        self.cid = cid
        self.backend = backend
        self.spec_path = spec_path
        self.trace_parent = trace_parent
        self.worker = worker
        self.tracer = tracer
        self.propagate_api = propagate_api
        self.status_cls = status_cls
        self.status_code_cls = status_code_cls
        self.span_kind_cls = span_kind_cls
        self._summary = OtelSummary(available=True, enabled=True)
        self._session_cm = None
        self._session_span = None
        self._closed = False

    def start_session(self) -> None:
        if self._session_span is not None or self._closed:
            return
        try:
            context = _extract_context(self.propagate_api, self.trace_parent)
            self._session_cm = self.tracer.start_as_current_span(
                "ooptdd.pytest.session",
                context=context,
                kind=self.span_kind_cls.INTERNAL,
                attributes={
                    "ooptdd.cid": self.cid,
                    "ooptdd.backend": self.backend,
                    "ooptdd.spec": self.spec_path,
                    "ooptdd.worker": self.worker,
                },
            )
            self._session_span = self._session_cm.__enter__()
            self._summary.spans_created += 1
        except Exception as exc:  # pragma: no cover - defensive around optional deps
            self._record_error(exc)

    def record_test_stage(
        self,
        *,
        nodeid: str,
        stage: str,
        outcome: str,
        duration: float,
    ) -> None:
        self._with_span(
            "ooptdd.pytest.test",
            attributes={
                "ooptdd.cid": self.cid,
                "ooptdd.spec": self.spec_path,
                "pytest.nodeid": nodeid,
                "pytest.stage": stage,
                "pytest.outcome": outcome,
                "pytest.duration_s": float(duration),
                "ooptdd.worker": self.worker,
            },
            ok=outcome == "passed",
        )

    def record_run_result(self, run_result) -> None:
        for requirement in getattr(run_result, "results", []):
            self._with_span(
                "ooptdd.requirement",
                attributes={
                    "ooptdd.cid": self.cid,
                    "ooptdd.backend": getattr(run_result, "backend", self.backend),
                    "ooptdd.requirement.id": requirement.id,
                    "ooptdd.requirement.description": requirement.description,
                    "ooptdd.requirement.gate_ok": bool(requirement.gate_ok),
                    "ooptdd.requirement.reachable": bool(requirement.reachable),
                    "ooptdd.requirement.bound": bool(requirement.bound),
                    "ooptdd.requirement.done": bool(requirement.done),
                },
                ok=bool(requirement.done),
            )

    def finish_session(self, run_result=None, *, error: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session_span is not None:
            try:
                if run_result is not None:
                    self._session_span.set_attribute("ooptdd.complete", bool(run_result.complete))
                    self._session_span.set_attribute("ooptdd.done", int(run_result.n_done))
                    self._session_span.set_attribute("ooptdd.total", len(run_result.results))
                    self._session_span.set_attribute(
                        "ooptdd.methodology_ok",
                        bool(run_result.methodology_ok),
                    )
                    if not run_result.complete:
                        self._session_span.set_status(
                            self.status_cls(self.status_code_cls.ERROR)
                        )
                if error:
                    self._session_span.set_attribute("ooptdd.error", error)
                    self._session_span.set_status(self.status_cls(self.status_code_cls.ERROR))
            except Exception as exc:  # pragma: no cover - defensive around optional deps
                self._record_error(exc)
        if self._session_cm is not None:
            try:
                self._session_cm.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover - defensive around optional deps
                self._record_error(exc)

    def summary(self) -> dict[str, Any]:
        return self._summary.__dict__.copy()

    def _with_span(self, name: str, *, attributes: dict[str, Any], ok: bool) -> None:
        try:
            with self.tracer.start_as_current_span(
                name,
                kind=self.span_kind_cls.INTERNAL,
                attributes=attributes,
            ) as span:
                self._summary.spans_created += 1
                if not ok:
                    span.set_status(self.status_cls(self.status_code_cls.ERROR))
        except Exception as exc:  # pragma: no cover - defensive around optional deps
            self._record_error(exc)

    def _record_error(self, exc: Exception) -> None:
        if not self._summary.error:
            self._summary.error = f"{type(exc).__name__}: {exc}"


def create_otel_recorder(
    *,
    cid: str,
    backend: str,
    spec_path: str,
    trace_parent: str | None = None,
    worker: bool = False,
    tracer=None,
):
    try:
        from opentelemetry import propagate, trace
        from opentelemetry.trace import SpanKind, Status, StatusCode
    except ModuleNotFoundError:
        return NullOtelRecorder()
    except Exception as exc:
        return NullOtelRecorder(error=f"{type(exc).__name__}: {exc}")

    tracer = tracer or trace.get_tracer("ooptdd-loop")
    return OtelRecorder(
        cid=cid,
        backend=backend,
        spec_path=spec_path,
        trace_parent=trace_parent,
        worker=worker,
        tracer=tracer,
        propagate_api=propagate,
        status_cls=Status,
        status_code_cls=StatusCode,
        span_kind_cls=SpanKind,
    )


def _extract_context(propagate, trace_parent: str | None):
    if not trace_parent:
        return None
    return propagate.extract({"traceparent": trace_parent})
