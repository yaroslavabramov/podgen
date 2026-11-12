from decimal import Decimal

from ..exchange_history import attempts, revisions


def state(journal, key):
    parsed = revisions(journal, key)
    if parsed and parsed[-1]["result"]:
        return "part_generated"
    if parsed and parsed[-1]["failure"]:
        return "processing_failed"
    receipt = journal.get(key + ":receipt")
    if receipt:
        if not receipt["complete"] or receipt["error"]:
            return "outcome_unknown"
        return "receipt_saved" if 200 <= receipt["status_code"] < 300 else "provider_rejected"
    return "outcome_unknown" if journal.get(key + ":submit") else "prepared"


def ledger(journal, key):
    count, cost = 0, Decimal(0)
    for row in attempts(journal, key):
        if not row["intent"]:
            continue
        count += 1
        results = [p["result"] for p in row["processing"] if p["result"]]
        known = results[-1]["cost"].get("usd") if results else None
        cost += Decimal(known if known is not None else row["intent"]["reservation_usd"])
    return count, cost
