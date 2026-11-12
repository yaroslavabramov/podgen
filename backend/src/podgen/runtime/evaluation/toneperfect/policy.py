from podgen.domain.errors import ContractError

from .observations import charged_or_reserved


def guard(store, config, case, histories):
    booked = sum(charged_or_reserved(c, rows) for c, rows in histories if c != case)
    if booked + case["reservation_credits"] > config.max_credits:
        raise ContractError("toneperfect_credit_cap")
    for previous, rows in histories:
        for row in rows:
            if store.get(row["key"] + "/model-change.json") or (
                row["result"] and row["result"]["policy_violations"]
            ):
                raise ContractError("toneperfect_previous_policy_violation")
        if rows and not rows[-1]["result"] and previous != case:
            raise ContractError("toneperfect_unresolved_previous_request")
        for row in rows:
            receipt = row["receipt"]
            if not receipt:
                continue
            free = receipt["headers"].get("x-toneperfect-free-credits", "")
            if row["result"]:
                free = row["result"]["provider_response"]["billing"].get("free_credits", free)
            if isinstance(free, str) and free.isascii() and free.isdecimal():
                free = int(free)
            if type(free) is not int or free < 0:
                continue
            later = sum(
                c["reservation_credits"]
                for c, history in histories
                if history
                and c != previous
                and history[0]["intent"]["submitted_at"] > row["intent"]["submitted_at"]
            )
            if (previous != case or not row["result"]) and free - later < case["reservation_credits"]:
                raise ContractError("toneperfect_free_balance_insufficient")


def model_changed(result, histories):
    current = result["provider_response"].get("model_version")
    prior = {
        row["result"]["provider_response"].get("model_version")
        for _, rows in histories
        for row in rows
        if row["result"]
    }
    prior.discard(None)
    return bool(current and prior and current not in prior)
