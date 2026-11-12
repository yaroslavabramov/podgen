import json

from podgen.audio.toneperfect import parse
from podgen.domain.errors import ContractError


def process(store, case, row):
    receipt = row["receipt"]
    if not receipt or not receipt["complete"] or receipt["error"] or receipt["status_code"] != 200:
        return None
    if row["result"] or row["failure"]:
        return row["result"]
    try:
        value = parse(
            store.bytes(row["key"] + "/response.bin"), receipt["headers"], case["reservation_credits"]
        )
        store.put(row["key"] + "/result.json", value)
        return value
    except (ValueError, TypeError, KeyError, ContractError) as error:
        store.put(
            row["key"] + "/failure.json", dict(status="response_invalid", error_type=type(error).__name__)
        )
        return None


def charged_or_reserved(case, rows):
    known = [row["result"]["provider_response"]["billing"]["credits"] for row in rows if row["result"]]
    for row in rows:
        receipt = row["receipt"]
        if receipt:
            header = receipt["headers"].get("x-toneperfect-credits-charged", "")
            if header.isascii() and header.isdecimal():
                known.append(int(header))
    if not any(row["result"] for row in rows) and rows:
        known.append(case["reservation_credits"])
    return max(known, default=0)


def retryable(store, row):
    receipt = row["receipt"]
    if row["result"] or row["failure"]:
        return False
    if not receipt or not receipt["complete"] or receipt["error"]:
        return True
    if receipt["status_code"] in (408, 429) or 500 <= receipt["status_code"] < 600:
        return True
    if receipt["status_code"] == 409:
        try:
            value = json.loads(store.bytes(row["key"] + "/response.bin"))
            return value["error"]["code"] == "request_pending"
        except (ValueError, KeyError, TypeError):
            return False
    return False
