from dataclasses import dataclass
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from ooptdd_loop import golden
from ooptdd_loop.engine.selector_gates import evaluate_gate

EVENT_NAMES = ("order_received", "payment_authorized", "order_shipped", "fraud_checked")
SERVICES = ("web", "billing", "fulfillment", "fraud")
OPS = ("==", "!=", ">=", ">", "<=", "<")


@dataclass
class _Backend:
    events: list[dict]
    reachable: bool = True
    default_lookback_s: int = 3600
    default_future_buffer_s: int = 0

    def query(self, cid, *, since_us, until_us):
        return SimpleNamespace(reachable=self.reachable, events=self.events)


def _event_strategy():
    return st.builds(
        lambda idx, event, service, amount: {
            "_timestamp": idx,
            "cid": "fuzz",
            "event": event,
            "service": service,
            "amount": amount,
        },
        idx=st.integers(min_value=1, max_value=1_000_000),
        event=st.sampled_from(EVENT_NAMES),
        service=st.sampled_from(SERVICES),
        amount=st.integers(min_value=0, max_value=5),
    )


def _selector_strategy():
    return st.fixed_dictionaries(
        {
            "event": st.sampled_from(EVENT_NAMES),
            "service": st.sampled_from(SERVICES),
            "attrs": st.fixed_dictionaries(
                {"amount": st.integers(min_value=0, max_value=5)}
            ),
        }
    )


def _matches(event: dict, selector: dict) -> bool:
    return (
        event.get("event") == selector["event"]
        and event.get("service") == selector["service"]
        and event.get("amount") == selector["attrs"]["amount"]
    )


def _op_result(op: str, got: int, want: int) -> bool:
    return {
        "==": got == want,
        "!=": got != want,
        ">=": got >= want,
        ">": got > want,
        "<=": got <= want,
        "<": got < want,
    }[op]


@settings(max_examples=80, deadline=None)
@given(
    events=st.lists(_event_strategy(), max_size=12),
    selector=_selector_strategy(),
    op=st.sampled_from(OPS),
    want=st.integers(min_value=0, max_value=12),
)
def test_selector_count_matches_manual_cardinality(events, selector, op, want):
    spec = {"cid": "fuzz", "expect": [{"select": selector, "op": op, "count": want}]}

    out = evaluate_gate(_Backend(events), spec)

    got = sum(1 for event in events if _matches(event, selector))
    check = out["checks"][0]
    assert check["got"] == got
    assert check["passed"] is _op_result(op, got, want)
    assert out["ok"] is check["passed"]


def _stream_strategy():
    return st.lists(st.sampled_from(EVENT_NAMES), max_size=10)


def _order_strategy():
    return st.lists(st.sampled_from(EVENT_NAMES), min_size=1, max_size=5)


def _manual_firsts(stream: list[str], order: list[str]) -> list[int | None]:
    firsts = []
    for expected in order:
        try:
            firsts.append(stream.index(expected))
        except ValueError:
            firsts.append(None)
    return firsts


@settings(max_examples=80, deadline=None)
@given(stream=_stream_strategy(), order=_order_strategy())
def test_selector_order_matches_first_occurrence_semantics(stream, order):
    events = [
        {"_timestamp": idx + 1, "cid": "fuzz", "event": event, "service": "svc"}
        for idx, event in enumerate(stream)
    ]
    selectors = [{"event": event, "service": "svc"} for event in order]
    spec = {"cid": "fuzz", "expect": [{"must_order": selectors}]}

    out = evaluate_gate(_Backend(events), spec)

    firsts = _manual_firsts(stream, order)
    manual_passed = all(first is not None for first in firsts) and all(
        firsts[i] <= firsts[i + 1] for i in range(len(firsts) - 1)
    )
    check = out["checks"][0]
    assert check["passed"] is manual_passed
    assert out["ok"] is manual_passed


@settings(max_examples=60, deadline=None)
@given(
    baseline_events=st.lists(_event_strategy(), max_size=8),
    current_events=st.lists(_event_strategy(), max_size=8),
    current_complete=st.booleans(),
)
def test_golden_status_priority_is_deterministic(
    baseline_events,
    current_events,
    current_complete,
):
    baseline = _snapshot(baseline_events, complete=True)
    current = _snapshot(current_events, complete=current_complete)

    changes = golden._changes(baseline, current)
    status = golden._status(current, changes)

    assert status in {"PASSED", "TOOLS_CHANGED", "OUTPUT_CHANGED", "REGRESSION"}
    if not current_complete:
        assert status == "REGRESSION"
    elif baseline["event_identities"] != current["event_identities"]:
        assert status == "TOOLS_CHANGED"
    elif baseline["events"] != current["events"]:
        assert status == "OUTPUT_CHANGED"
    else:
        assert status == "PASSED"


def _snapshot(events: list[dict], *, complete: bool) -> dict:
    return {
        "complete": complete,
        "requirements": [
            {
                "id": "REQ",
                "gate_ok": complete,
                "bound": True,
                "done": complete,
            }
        ],
        "event_identities": [golden._event_identity(event) for event in events],
        "events": [golden._normalize_event(event) for event in events],
    }
