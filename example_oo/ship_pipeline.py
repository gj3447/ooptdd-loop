#!/usr/bin/env python3
"""R1 demo — a standalone process that ships pipeline events to a real OpenObserve.

Run separately from the loop; the loop then verifies these events arrived by
querying oo — the genuine cross-process, real-backend path.

env: OOPTDD_CID (correlation id), OOPTDD_OO_URL / OOPTDD_OO_USER / OOPTDD_OO_PASSWORD
(secrets, env-only), OOPTDD_OO_STREAM (default ooptdd_demo).
"""
import os
import sys

from ooptdd.backends import get_backend


def main():
    cid = os.environ["OOPTDD_CID"]
    stream = os.getenv("OOPTDD_OO_STREAM", "ooptdd_demo")
    backend = get_backend("openobserve", stream=stream)

    def ev(name, **attrs):
        return {"cid": cid, "correlation_id": cid, "cycle_id": cid,
                "service": "shop.oo", "event": name, **attrs}

    backend.ship([ev("order_received", items=2)])
    backend.ship([ev("payment_authorized", amount=10.0)])
    backend.ship([ev("order_shipped")])
    print(f"shipped 3 events to oo stream={stream} cid={cid}", file=sys.stderr)


if __name__ == "__main__":
    main()
