"""Example SUT that emits structured events through Python logging."""
from __future__ import annotations

import logging

logger = logging.getLogger("checkout")


def run_pipeline(_backend, _cid):
    logger.info(
        "payment authorized",
        extra={
            "event": "payment_authorized",
            "operation": "authorize",
            "amount": 42,
        },
    )
    logger.info(
        {
            "event": "order_shipped",
            "service": "fulfillment",
            "operation": "ship",
            "tracking_no": "T-1",
        }
    )
