"""One window can have explicitly requested paid attempts; resume never adds one."""

from .processing import revisions


def history(journal, window_key):
    rows = []
    number = 1
    while journal.get(f"{window_key}:attempt:{number}:prepared") is not None:
        key = f"{window_key}:attempt:{number}"
        rows.append(
            dict(
                key=key,
                number=number,
                prepared=journal.get(key + ":prepared"),
                intent=journal.get(key + ":submit"),
                receipt=journal.get(key + ":receipt"),
                processing=revisions(journal, key),
            )
        )
        number += 1
    return rows


def exchange_state(journal, key):
    parsed = revisions(journal, key)
    if parsed and parsed[-1]["result"]:
        return "reviewed"
    if parsed and parsed[-1]["failure"]:
        return "response_invalid"
    receipt = journal.get(key + ":receipt")
    if receipt:
        if not receipt["complete"] or receipt["error"]:
            return "outcome_unknown"
        return "receipt_saved" if 200 <= receipt["status_code"] < 300 else "provider_rejected"
    return "outcome_unknown" if journal.get(key + ":submit") else "prepared"


def state(journal, window_key):
    rows = history(journal, window_key)
    return exchange_state(journal, rows[-1]["key"]) if rows else "prepared"
