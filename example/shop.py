"""Tiny system-under-test: an order pipeline that emits a trace event per step.

The Longinus bindings in ``requirements.yaml`` point at these symbols by name, so
the loop can prove the GREEN gate was satisfied *by this code* and not by accident.
"""
from __future__ import annotations


def _ev(cid, event, **attrs):
    return {"cid": cid, "correlation_id": cid, "cycle_id": cid,
            "service": "shop", "event": event, **attrs}


def authorize_payment(backend, cid, amount=42.0):
    # emits the amount so the event conforms to the PaymentAuthorized ontology type
    backend.ship([_ev(cid, "payment_authorized", amount=amount)])


def pack_items(backend, cid, n):
    for i in range(n):
        backend.ship([_ev(cid, "line_item_packed", index=i)])


def run_pipeline(backend, cid):
    """Entry point the loop calls: process one order under correlation id ``cid``."""
    backend.ship([_ev(cid, "order_received", items=3)])
    authorize_payment(backend, cid)
    pack_items(backend, cid, 3)
    backend.ship([_ev(cid, "order_shipped")])
    return {"status": "ok"}
