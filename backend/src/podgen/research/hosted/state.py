from podgen.domain.errors import ContractError

from .provider import snapshot


def exchanges(journal):
    index = 1
    while journal.get(f"exchange:{index}:intent"):
        yield f"exchange:{index}"
        index += 1


def latest(journal):
    """A failed GET never erases the most recent known provider job."""
    found = None
    for key in exchanges(journal):
        receipt = journal.get(key + ":receipt")
        if not receipt or not receipt["complete"] or receipt["error"]:
            continue
        if receipt["status_code"] is None or not 200 <= receipt["status_code"] < 300:
            continue
        try:
            value = snapshot(journal.bytes(key + ":receipt", receipt["raw_response_blob"]))
        except (ValueError, TypeError, ContractError):
            continue
        if found and found[1]["id"] != value["id"]:
            raise ContractError("hosted_research_provider_job_changed")
        found = key, value
    return found


def status(journal):
    result = journal.get("result")
    current = latest(journal)
    value = dict(
        run_id=str(journal.run_id),
        approved=journal.get("approval") is not None,
        submissions=sum(journal.get(k + ":intent")["operation"] == "submit" for k in exchanges(journal)),
        provider_job_id=current[1]["id"] if current else None,
        usage=current[1].get("usage") if current else None,
        cost_usd=None,
        cost_basis="provider_invoice_not_available",
    )
    if result:
        value.update(state="completed", result_ref=journal.ref("result").model_dump(mode="json"))
    elif current:
        value["state"] = current[1]["status"]
        if current[1]["status"] == "completed":
            value["state"] = "result_processing_required"
    elif value["submissions"]:
        keys = list(exchanges(journal))
        receipt = journal.get(keys[0] + ":receipt")
        value["state"] = "outcome_unknown"
        if (
            receipt
            and receipt["complete"]
            and not receipt["error"]
            and receipt["status_code"] in (400, 401, 403, 404, 405, 413, 415, 422, 429)
        ):
            value["state"] = "provider_rejected"
    else:
        value["state"] = "prepared"
    return value
