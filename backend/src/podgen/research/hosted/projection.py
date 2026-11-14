"""Bounded derived state of one immutable hosted-research exchange journal.

The project/run owns one head, anchored to its immutable manifest. Exchange
records remain the authority; each intent/receipt advances the head in the same
transaction. No previous provider bodies or list of exchanges lives in this row.
"""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .provider import snapshot

CONTRACT = "hosted-research-head/1"
REJECTED = (400, 401, 403, 404, 405, 413, 415, 422, 429)


def empty():
    return dict(
        contract=CONTRACT,
        revision=0,
        exchanges=0,
        submissions=0,
        last_exchange=None,
        current=None,
        job_recorded=False,
        submit_rejected=False,
    )


def intent_head(previous, operation):
    if operation not in ("submit", "get", "cancel"):
        raise ContractError("hosted_research_unknown_exchange_operation")
    if operation == "submit" and previous["exchanges"]:
        raise ContractError("hosted_research_duplicate_submission")
    value = deepcopy(previous)
    value["revision"] += 1
    value["exchanges"] += 1
    value["submissions"] += operation == "submit"
    value["last_exchange"] = dict(key=f"exchange:{value['exchanges']}", operation=operation, receipt=False)
    return value


def receipt_head(previous, key, receipt, raw):
    last = previous["last_exchange"]
    if not last or last["key"] != key or last["receipt"]:
        raise ContractError("hosted_research_receipt_without_pending_intent")
    value = deepcopy(previous)
    value["revision"] += 1
    valid = receipt["complete"] and not receipt["error"]
    code = receipt["status_code"]
    value["last_exchange"].update(
        receipt=True,
        status_code=code,
        complete=receipt["complete"],
        error=receipt["error"],
    )
    if last["operation"] == "submit":
        value["submit_rejected"] = bool(valid and code in REJECTED)
    if valid and code is not None and 200 <= code < 300:
        try:
            parsed = snapshot(raw)
        except (ValueError, TypeError, ContractError):
            pass
        else:
            current = value["current"]
            if current and current["id"] != parsed["id"]:
                value["integrity_error"] = "hosted_research_provider_job_changed"
            else:
                value["current"] = dict(
                    key=key,
                    id=parsed["id"],
                    status=parsed["status"],
                    usage=parsed.get("usage"),
                )
                value["job_recorded"] = True
    return value
