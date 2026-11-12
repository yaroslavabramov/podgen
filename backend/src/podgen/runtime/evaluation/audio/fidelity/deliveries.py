"""Original ASR exchange keeps its historical key; explicit retries add separate exchanges."""

from decimal import Decimal

from pydantic import Field

from podgen.domain.contracts.common import Contract, DecimalString, Text

from .contracts import prefix


class RetryPolicy(Contract):
    reason: Text
    max_total_requests: int = Field(ge=2, le=3)
    max_total_cost_usd: DecimalString


def deliveries(journal, config):
    root = prefix(config.audit_id)
    rows = [dict(key=root, authorization=None)]
    n = 1
    while journal.get(f"{root}:retry:{n}:authorization") is not None:
        key = f"{root}:retry:{n}"
        rows.append(dict(key=key, authorization=journal.get(key + ":authorization")))
        n += 1
    return [
        dict(r, intent=journal.get(r["key"] + ":submit"), receipt=journal.get(r["key"] + ":receipt"))
        for r in rows
    ]


def latest(journal, config):
    return deliveries(journal, config)[-1]


def reserved(journal, config):
    rows = deliveries(journal, config)
    return sum(bool(r["intent"]) for r in rows), sum(
        (Decimal(r["intent"]["reservation_usd"]) for r in rows if r["intent"]), Decimal(0)
    )
