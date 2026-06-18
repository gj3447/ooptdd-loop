"""Selector-aware gate evaluation for OOPTDD loop specs.

The upstream ``ooptdd.gate`` evaluator is intentionally small: event + where
matching, ordering by event name, and ontology conformance. This module keeps
that evaluator as the default path and adds the OOPTDD selector DSL on top.
"""
from __future__ import annotations

import operator
import time
from dataclasses import dataclass
from typing import Any

from ooptdd.gate import evaluate as evaluate_ooptdd_gate

_OPS = {
    "==": operator.eq,
    "=": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
}

_SELECTOR_KEYS = {"select", "selector", "after", "predecessor"}
_SELECTOR_RESERVED = {"event", "name", "type", "where", "attrs", "attributes"}


@dataclass(frozen=True)
class _Events:
    reachable: bool
    events: list[dict]


def evaluate_gate(backend, spec: dict, *, ontology=None) -> dict:
    """Evaluate one gate spec, adding support for selector rules.

    Non-selector rules are delegated rule-by-rule to ``ooptdd.gate.evaluate`` so
    existing behavior remains unchanged. Selector rules are evaluated against the
    same backend query shape and return the same result contract.
    """
    cid = spec["cid"]
    checks: list[dict] = []
    reachable = True
    queried: _Events | None = None
    non_selector: list[dict] = []

    for rule in spec.get("expect", []):
        if _is_selector_rule(rule):
            if queried is None:
                queried = _query_events(backend, cid)
            chk = _eval_selector_rule(queried.events, rule, queried.reachable)
            checks.append(chk)
            reachable = reachable and queried.reachable
            continue
        non_selector.append(rule)

    # Delegate ALL non-selector rules in ONE call so spec-level keys survive — notably
    # `forbid_errors`/`allow_errors`/`error_levels` (the negative wing) and `indicators`/
    # `threshold`. A per-rule sub-spec dropped them, which (a) re-injected the env
    # error-forbid once per rule and (b) injected it ZERO times when every rule was a
    # selector. One call fires the injection exactly once, even with no non-selector rules.
    delegated = {k: spec[k] for k in (
        "indicators", "threshold", "forbid_errors", "allow_errors", "error_levels",
        "ontology", "timeWindow", "time_window") if k in spec}
    delegated["cid"] = cid
    delegated["expect"] = non_selector
    out = evaluate_ooptdd_gate(backend, delegated, ontology=ontology)
    checks.extend(out["checks"])
    reachable = reachable and out["reachable"]

    gating = [c for c in checks if not c["optional"] and not c["pending"]]
    required_ok = all(c["passed"] for c in gating)
    return {
        "ok": reachable and required_ok,
        "reachable": reachable,
        "cid": cid,
        "checks": checks,
        "optional_failed": [_label(c) for c in checks if c["optional"] and not c["passed"]],
        "pending_failed": [_label(c) for c in checks if c["pending"] and not c["passed"]],
        "pending_satisfied": [_label(c) for c in checks if c["pending"] and c["passed"]],
    }


def is_selector_rule(rule: dict) -> bool:
    return _is_selector_rule(rule)


def selector_event_names(rule: dict) -> list[str]:
    names: list[str] = []

    def add(selector: Any) -> None:
        event = _selector_event(_normalize_selector(selector))
        if event and event not in names:
            names.append(event)

    if "must_order" in rule or "trajectory" in rule:
        for part in rule.get("must_order") or rule.get("trajectory") or []:
            add(part)
    for key in ("select", "selector", "after", "predecessor"):
        if key in rule:
            add(rule[key])
    return names


def _is_selector_rule(rule: dict) -> bool:
    if any(key in rule for key in _SELECTOR_KEYS):
        return True
    seq = rule.get("must_order") or rule.get("trajectory") or []
    return any(isinstance(part, dict) for part in seq)


def _query_events(backend, cid: str) -> _Events:
    now_us = int(time.time() * 1_000_000)
    lookback_s = backend.default_lookback_s
    future_buffer_s = backend.default_future_buffer_s
    result = backend.query(
        cid,
        since_us=now_us - lookback_s * 1_000_000,
        until_us=now_us + future_buffer_s * 1_000_000,
    )
    return _Events(reachable=result.reachable, events=result.events)


def _eval_selector_rule(events: list[dict], rule: dict, reachable: bool) -> dict:
    if "must_order" in rule or "trajectory" in rule:
        return _eval_selector_order(events, rule, reachable)
    return _eval_selector_count_or_causal(events, rule, reachable)


def _eval_selector_count_or_causal(events: list[dict], rule: dict, reachable: bool) -> dict:
    selector = _normalize_selector(rule.get("select", rule.get("selector", {})))
    where = _selector_where(selector)
    matches = _matching_events(events, selector)
    op = _norm_op(rule.get("op", ">="))
    want = int(rule.get("count", rule.get("want", 1)))
    got = len(matches)
    count_passed = reachable and _OPS[op](got, want)
    label = _selector_label(selector)
    chk = {
        "select": selector,
        "label": label,
        "event": _selector_event(selector),
        "where": where,
        "op": op,
        "want": want,
        "got": got,
        "passed": count_passed,
    }

    predecessor = rule.get("after", rule.get("predecessor"))
    if predecessor is not None:
        pred = _normalize_selector(predecessor)
        causal = _causal_check(events, pred, selector, within_s=rule.get("within_s"))
        chk["after"] = pred
        chk["causal"] = causal
        chk["passed"] = count_passed and causal["passed"]

    _attach_gate_metadata(chk, rule)
    return chk


def _eval_selector_order(events: list[dict], rule: dict, reachable: bool) -> dict:
    selectors = [_normalize_selector(part) for part in rule.get("must_order", rule.get("trajectory"))]
    labels = [_selector_label(selector) for selector in selectors]
    firsts = [(label, _first_ts(events, selector)) for label, selector in zip(labels, selectors)]
    missing = [label for label, ts in firsts if ts is None]
    ordered = not missing and all(
        firsts[i][1] <= firsts[i + 1][1] for i in range(len(firsts) - 1)
    )
    gaps_exceeded: list[str] = []
    within_s = rule.get("within_s")
    if ordered and within_s is not None:
        bound_us = float(within_s) * 1_000_000
        for i in range(len(firsts) - 1):
            if firsts[i + 1][1] - firsts[i][1] > bound_us:
                gaps_exceeded.append(f"{firsts[i][0]}->{firsts[i + 1][0]}")
    chk = {
        "selector_order": labels,
        "selectors": selectors,
        "missing": missing,
        "ordered": ordered,
        "firsts": {label: ts for label, ts in firsts},
        "passed": reachable and ordered and not gaps_exceeded,
    }
    if within_s is not None:
        chk["within_s"] = float(within_s)
        chk["gaps_exceeded"] = gaps_exceeded
    _attach_gate_metadata(chk, rule)
    return chk


def _causal_check(
    events: list[dict],
    predecessor: dict,
    target: dict,
    *,
    within_s: float | None,
) -> dict:
    pred_ts = [_ts(ev) for ev in _matching_events(events, predecessor)]
    target_ts = [_ts(ev) for ev in _matching_events(events, target)]
    bound_us = None if within_s is None else float(within_s) * 1_000_000
    pairs = []
    for p_ts in pred_ts:
        for t_ts in target_ts:
            if p_ts is None or t_ts is None or p_ts > t_ts:
                continue
            if bound_us is not None and t_ts - p_ts > bound_us:
                continue
            pairs.append((p_ts, t_ts))
    return {
        "predecessor": _selector_label(predecessor),
        "target": _selector_label(target),
        "predecessor_found": bool(pred_ts),
        "target_found": bool(target_ts),
        "within_s": within_s,
        "passed": bool(pairs),
    }


def _normalize_selector(selector: Any) -> dict:
    if isinstance(selector, str):
        return {"event": selector}
    return dict(selector or {})


def _selector_event(selector: dict) -> str | None:
    return selector.get("event") or selector.get("name") or selector.get("type")


def _selector_where(selector: dict) -> dict:
    where = {
        key: value
        for key, value in selector.items()
        if key not in _SELECTOR_RESERVED
    }
    where.update(selector.get("where") or {})
    where.update(selector.get("attrs") or {})
    where.update(selector.get("attributes") or {})
    return where


def _matching_events(events: list[dict], selector: dict) -> list[dict]:
    event = _selector_event(selector)
    where = _selector_where(selector)
    return [
        ev for ev in events
        if (event is None or ev.get("event") == event or ev.get("event_type") == event)
        and all(ev.get(key) == value for key, value in where.items())
    ]


def _first_ts(events: list[dict], selector: dict) -> int | None:
    for ev in _matching_events(events, selector):
        ts = _ts(ev)
        if ts is not None:
            return ts
    return None


def _ts(event: dict) -> int | None:
    value = event.get("_timestamp")
    return int(value) if value is not None else None


def _selector_label(selector: dict) -> str:
    event = _selector_event(selector) or "*"
    where = _selector_where(selector)
    if not where:
        return event
    suffix = ",".join(f"{key}={value}" for key, value in sorted(where.items()))
    return f"{event}[{suffix}]"


def _norm_op(op: str) -> str:
    if op not in _OPS:
        raise ValueError(f"unknown selector gate op {op!r}; expected one of {sorted(_OPS)}")
    return op


def _attach_gate_metadata(check: dict, rule: dict) -> None:
    check["optional"] = bool(rule.get("optional", False))
    check["pending"] = bool(rule.get("pending", False))
    check["weight"] = float(rule.get("weight", 1.0))


def _label(check: dict) -> str:
    if "selector_order" in check:
        return "selector_order:" + ">".join(check["selector_order"])
    if "select" in check:
        return "selector:" + check["label"]
    if "conforms" in check:
        return "conforms:" + str(check["conforms"])
    if "must_order" in check:
        return "must_order:" + ">".join(check["must_order"])
    return str(check.get("event") or check.get("where") or "(any)")
