"""Current operation outcome is separate from the immutable provider-delivery history."""

from ..exchange_history import attempts
from .attempts import ledger, state
from .contracts import prefix


def latest(journal, config):
    number = 1
    value = None
    while journal.get(f"{prefix(config.repair_id)}:dispatch:{number}") is not None:
        value = journal.get(f"{prefix(config.repair_id)}:dispatch:{number}")
        number += 1
    return value


def record(journal, config, status, *, result_ref=None, reason=None):
    # Caller owns the comparison lock.
    root = prefix(config.repair_id)
    number = 1
    while journal.get(f"{root}:dispatch:{number}") is not None:
        number += 1
    count, cost = ledger(journal, root)
    journal.put(
        f"{root}:dispatch:{number}",
        dict(
            contract="fragment-repair-dispatch/1",
            status=status,
            reason=reason,
            manifest_ref=journal.ref(root + ":manifest").model_dump(mode="json"),
            result_ref=result_ref.model_dump(mode="json") if result_ref else None,
            delivery_stamp=delivery_stamp(journal, config),
            requests_submitted=count,
            cost_or_reservation_usd=str(cost),
        ),
    )
    return status


def delivery_stamp(journal, config):
    rows = attempts(journal, prefix(config.repair_id))
    if not rows:
        return dict(attempt_ref=None, submit_ref=None, processing_ref=None, delivery_state="not_submitted")
    row = rows[-1]
    parsed = row["processing"][-1] if row["processing"] else None
    return dict(
        attempt_ref=journal.ref(row["key"] + ":prepared").model_dump(mode="json"),
        submit_ref=journal.ref(row["key"] + ":submit").model_dump(mode="json") if row["intent"] else None,
        processing_ref=journal.ref(parsed["key"]).model_dump(mode="json") if parsed else None,
        delivery_state=state(journal, row["key"]),
    )
