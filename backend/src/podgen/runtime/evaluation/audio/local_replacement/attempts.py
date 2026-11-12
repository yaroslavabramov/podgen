from decimal import Decimal

from ..exchange_history import attempts, revisions


def state(journal, key):
    rows = revisions(journal, key)
    if rows and rows[-1]["result"]:
        return "synthesized"
    if rows and rows[-1]["failure"]:
        return "processing_failed"
    receipt = journal.get(key + ":receipt")
    if receipt:
        if not receipt["complete"] or receipt["error"]:
            return "outcome_unknown"
        if 200 <= receipt["status_code"] < 300:
            return "receipt_saved"
        if receipt["status_code"] in (400, 401, 403, 404, 405, 413, 415, 422, 429):
            return "provider_rejected"
        return "outcome_unknown"
    return "outcome_unknown" if journal.get(key + ":submit") else "prepared"


def ledger(journal, key):
    count, cost = 0, Decimal(0)
    for row in attempts(journal, key):
        if row["intent"]:
            count += 1
            result = next((r["result"] for r in reversed(row["processing"]) if r["result"]), None)
            known = result["cost"].get("usd") if result else None
            cost += Decimal(known if known is not None else row["intent"]["reservation_usd"])
    return count, cost
