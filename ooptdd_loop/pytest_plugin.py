"""Pytest integration for OOPTDD.

The plugin does not run the system under test itself. Pytest is the runtime:
tests emit events under ``OOPTDD_CID`` and the plugin evaluates the configured
OOPTDD spec at session finish.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from ooptdd.backends import get_backend

from .report import next_step_context, render
from .runner import _new_cid, evaluate_requirements
from .otel import create_otel_recorder
from .spec import load_spec

PLUGIN_NAME = "ooptdd-loop-runtime"
ENV_KEYS = (
    "OOPTDD_CID",
    "OOPTDD_BACKEND",
    "OOPTDD_BACKEND_OPTIONS",
    "OOPTDD_SPEC",
    "TRACEPARENT",
)
FORWARDABLE_BACKENDS = {"memory"}


def worker_input_payload(
    *,
    cid: str,
    backend: str,
    spec: str,
    backend_options: dict[str, Any] | None = None,
    trace_parent: str | None = None,
) -> dict[str, str]:
    """Serializable environment payload for xdist workers."""
    payload = {
        "OOPTDD_CID": cid,
        "OOPTDD_BACKEND": backend,
        "OOPTDD_BACKEND_OPTIONS": json.dumps(backend_options or {}, sort_keys=True),
        "OOPTDD_SPEC": spec,
    }
    if trace_parent:
        payload["TRACEPARENT"] = trace_parent
    return payload


def is_xdist_worker(config) -> bool:
    return hasattr(config, "workerinput")


def pytest_addoption(parser) -> None:
    group = parser.getgroup("ooptdd-loop", "OOPTDD loop runtime")
    group.addoption(
        "--ooptdd-spec",
        action="store",
        default=None,
        help="Path to an OOPTDD requirements YAML evaluated after the pytest session.",
    )
    group.addoption(
        "--ooptdd-cid",
        action="store",
        default=None,
        help="Correlation id for events emitted by this pytest session.",
    )
    group.addoption(
        "--ooptdd-report",
        action="store",
        default=None,
        help="Optional JSON receipt path for the OOPTDD session verdict.",
    )
    group.addoption(
        "--ooptdd-trace-parent",
        action="store",
        default=None,
        help="Optional W3C traceparent to expose as TRACEPARENT to tests/workers.",
    )
    group.addoption(
        "--ooptdd-passes",
        action="store",
        type=int,
        default=1,
        help="Number of OOPTDD verdict evaluation passes after pytest finishes.",
    )
    group.addoption(
        "--ooptdd-pass-delay",
        action="store",
        type=float,
        default=0.0,
        help="Seconds to wait between OOPTDD evaluation passes.",
    )


def pytest_configure(config) -> None:
    if not config.getoption("--ooptdd-spec", default=None):
        return
    if config.pluginmanager.has_plugin(PLUGIN_NAME):
        return
    config.pluginmanager.register(OOPTDDPytestPlugin(config), PLUGIN_NAME)


class OOPTDDPytestPlugin:
    def __init__(self, config) -> None:
        self.config = config
        self.is_worker = is_xdist_worker(config)
        workerinput = getattr(config, "workerinput", {}) or {}
        self.spec_path = workerinput.get("OOPTDD_SPEC") or config.getoption("--ooptdd-spec")
        self.spec = load_spec(self.spec_path)
        self.cid = (
            workerinput.get("OOPTDD_CID")
            or config.getoption("--ooptdd-cid")
            or os.getenv("OOPTDD_CID")
            or _new_cid("pytest")
        )
        self.backend = workerinput.get("OOPTDD_BACKEND") or self.spec.target.backend
        self.backend_options = _backend_options(
            workerinput.get("OOPTDD_BACKEND_OPTIONS"),
            default=self.spec.target.backend_options,
        )
        self.trace_parent = (
            workerinput.get("TRACEPARENT")
            or config.getoption("--ooptdd-trace-parent")
            or os.getenv("TRACEPARENT")
        )
        self.report_path = config.getoption("--ooptdd-report")
        self.passes = max(1, int(config.getoption("--ooptdd-passes") or 1))
        self.pass_delay = max(0.0, float(config.getoption("--ooptdd-pass-delay") or 0.0))
        self.evaluation_attempts = 0
        self.started_at = time.time()
        self.tests: dict[str, dict[str, dict[str, Any]]] = {}
        self.forwarded_events: list[dict[str, Any]] = []
        self.worker_summaries: list[dict[str, Any]] = []
        self.run_result = None
        self.error: str | None = None
        self._old_env: dict[str, str | None] = {}
        self._apply_env(
            worker_input_payload(
                cid=self.cid,
                backend=self.backend,
                backend_options=self.backend_options,
                spec=self.spec_path,
                trace_parent=self.trace_parent,
            )
        )
        self.otel = create_otel_recorder(
            cid=self.cid,
            backend=self.backend,
            spec_path=self.spec_path,
            trace_parent=self.trace_parent,
            worker=self.is_worker,
        )
        self.otel.start_session()

    def _apply_env(self, env: dict[str, str]) -> None:
        for key, value in env.items():
            if key not in self._old_env:
                self._old_env[key] = os.environ.get(key)
            os.environ[key] = value

    def _restore_env(self) -> None:
        for key in ENV_KEYS:
            if key not in self._old_env:
                continue
            old = self._old_env[key]
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node) -> None:
        node.workerinput.update(
            worker_input_payload(
                cid=self.cid,
                backend=self.backend,
                backend_options=self.backend_options,
                spec=self.spec_path,
                trace_parent=self.trace_parent,
            )
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        props = list(getattr(report, "user_properties", []))
        props.append(("ooptdd_cid", self.cid))
        props.append(("ooptdd_spec", self.spec_path))
        if self.trace_parent:
            props.append(("traceparent", self.trace_parent))
        report.user_properties = props

    def pytest_runtest_logreport(self, report) -> None:
        by_stage = self.tests.setdefault(report.nodeid, {})
        stage = {
            "outcome": report.outcome,
            "duration": getattr(report, "duration", 0.0),
        }
        if getattr(report, "longrepr", None):
            stage["longrepr"] = str(report.longrepr)
        by_stage[report.when] = stage
        self.otel.record_test_stage(
            nodeid=report.nodeid,
            stage=report.when,
            outcome=report.outcome,
            duration=getattr(report, "duration", 0.0),
        )

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error) -> None:
        workeroutput = getattr(node, "workeroutput", {}) or {}
        if not isinstance(workeroutput, dict):
            return
        if workeroutput.get("ooptdd_cid") != self.cid:
            return

        events = workeroutput.get("ooptdd_events") or []
        if events:
            self.forwarded_events.extend(events)
        self.worker_summaries.append(
            {
                "worker": _worker_id(node, workeroutput),
                "events": len(events),
                "trace_parent": workeroutput.get("ooptdd_trace_parent"),
                "otel": workeroutput.get("ooptdd_otel"),
                "error": str(error) if error else None,
            }
        )

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        if self.is_worker:
            workeroutput = getattr(self.config, "workeroutput", None)
            if isinstance(workeroutput, dict):
                workeroutput["ooptdd_cid"] = self.cid
                workeroutput["ooptdd_trace_parent"] = self.trace_parent
                workeroutput["ooptdd_events"] = self._collect_forwardable_events()
                self.otel.finish_session(error=None if exitstatus == 0 else str(exitstatus))
                workeroutput["ooptdd_otel"] = self.otel.summary()
            return

        try:
            self._replay_forwarded_events()
            for attempt in range(self.passes):
                self.evaluation_attempts = attempt + 1
                self.run_result = evaluate_requirements(self.spec, cid=self.cid)
                if self.run_result.complete or attempt == self.passes - 1:
                    break
                if self.pass_delay:
                    time.sleep(self.pass_delay)
            self.otel.record_run_result(self.run_result)
            self.otel.finish_session(self.run_result)
        except Exception as exc:  # pragma: no cover - defensive; visible in terminal/report
            self.error = f"{type(exc).__name__}: {exc}"
            self.otel.finish_session(error=self.error)
            session.exitstatus = 1
            self._write_report()
            return

        self._write_report()
        if not self.run_result.complete and session.exitstatus == 0:
            session.exitstatus = 1

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.is_worker:
            return
        terminalreporter.section("OOPTDD")
        if self.error:
            terminalreporter.write_line(f"ERROR {self.error}")
            return
        if self.run_result is None:
            terminalreporter.write_line("not evaluated")
            return
        terminalreporter.write_line(render(self.run_result))
        ctx = next_step_context(self.run_result)
        if ctx:
            terminalreporter.write_line("")
            terminalreporter.write_line(ctx)

    def pytest_unconfigure(self, config) -> None:
        self.otel.finish_session(error=self.error)
        self._restore_env()

    def _write_report(self) -> None:
        if not self.report_path:
            return
        path = Path(self.report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _collect_forwardable_events(self) -> list[dict[str, Any]]:
        if self.backend not in FORWARDABLE_BACKENDS:
            return []
        backend = get_backend(self.backend, **self.backend_options)
        now_us = int(time.time() * 1_000_000)
        lookback_s = getattr(backend, "default_lookback_s", 3600)
        future_buffer_s = getattr(backend, "default_future_buffer_s", 0)
        result = backend.query(
            self.cid,
            since_us=now_us - lookback_s * 1_000_000,
            until_us=now_us + future_buffer_s * 1_000_000,
        )
        if not result.reachable:
            return []
        return _jsonable_events(result.events)

    def _replay_forwarded_events(self) -> None:
        if self.backend not in FORWARDABLE_BACKENDS or not self.forwarded_events:
            return
        get_backend(self.backend, **self.backend_options).ship(self.forwarded_events)

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cid": self.cid,
            "backend": self.backend,
            "backend_options": self.backend_options,
            "spec": self.spec_path,
            "evaluation_attempts": self.evaluation_attempts,
            "trace_parent": self.trace_parent,
            "otel": self.otel.summary(),
            "xdist": {
                "worker": self.is_worker,
                "workers": self.worker_summaries,
                "forwarded_events": len(self.forwarded_events),
            },
            "duration": time.time() - self.started_at,
            "tests": self.tests,
        }
        if self.error:
            payload.update({"complete": False, "error": self.error, "done": 0, "total": 0})
            return payload
        if self.run_result is None:
            payload.update({"complete": False, "done": 0, "total": 0, "requirements": []})
            return payload
        payload.update(
            {
                "complete": self.run_result.complete,
                "done": self.run_result.n_done,
                "total": len(self.run_result.results),
                "methodology_ok": self.run_result.methodology_ok,
                "methodology_checks": [
                    dataclasses.asdict(c) for c in self.run_result.methodology_checks
                ],
                "requirements": [
                    {
                        "id": r.id,
                        "gate_ok": r.gate_ok,
                        "reachable": r.reachable,
                        "bound": r.bound,
                        "done": r.done,
                        "checks": r.checks,
                        "binding": dataclasses.asdict(r.binding) if r.binding else None,
                    }
                    for r in self.run_result.results
                ],
            }
        )
        return payload


def _jsonable_events(events: list[dict]) -> list[dict[str, Any]]:
    """Return an execnet/JSON-safe event list for xdist workeroutput."""
    return json.loads(json.dumps(events, default=str))


def _backend_options(raw: str | None, *, default: dict[str, Any]) -> dict[str, Any]:
    if raw is None:
        return dict(default)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return dict(default)
    return decoded if isinstance(decoded, dict) else dict(default)


def _worker_id(node, workeroutput: dict[str, Any]) -> str | None:
    workerinput = getattr(node, "workerinput", {}) or {}
    return (
        workeroutput.get("workerid")
        or workerinput.get("workerid")
        or getattr(getattr(node, "gateway", None), "id", None)
    )
