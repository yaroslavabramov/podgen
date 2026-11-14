"""A bounded operational head; full HTTP history belongs only to archive/export."""

from contextlib import nullcontext

from podgen.domain.errors import ContractError

from .projection import empty, intent_head, receipt_head
from .provider import snapshot


def head(journal, *, locked=False):
    saved = journal.head()
    if saved is not None:
        return saved
    with nullcontext() if locked else journal.lock(write=False):
        saved = journal.head()
        if saved is not None:
            return saved
        value = empty()
        # Once-only adoption of immutable records from the previous implementation.
        while (intent := journal.get(f"exchange:{value['exchanges'] + 1}:intent")) is not None:
            value = intent_head(value, intent["operation"])
            key = f"exchange:{value['exchanges']}"
            receipt = journal.get(key + ":receipt")
            if receipt is not None:
                raw = (
                    journal.bytes(key + ":receipt", receipt["raw_response_blob"])
                    if receipt["complete"]
                    and not receipt["error"]
                    and receipt["status_code"] is not None
                    and 200 <= receipt["status_code"] < 300
                    else b""
                )
                value = receipt_head(value, key, receipt, raw)
        if not value["exchanges"] and journal.get("manifest") is None:
            return value  # Preserve status of an unprepared run without an orphan projection.
        # One legacy lookup, never a per-poll lookup. A receipt may have committed
        # just before the old implementation crashed while publishing its job.
        job = journal.get("job")
        value["job_recorded"] = job is not None
        if job and (not value["current"] or job["provider_job_id"] != value["current"]["id"]):
            value["integrity_error"] = "hosted_research_provider_job_changed"
        return journal.adopt_head(value)


def exchanges(journal):
    for index in range(1, head(journal)["exchanges"] + 1):
        yield f"exchange:{index}"


def latest(journal, *, projection=None):
    saved = head(journal) if projection is None else projection
    if saved.get("integrity_error"):
        raise ContractError(saved["integrity_error"])
    current = saved["current"]
    if current is None:
        return None
    if current["status"] == "completed":
        key = current["key"]
        receipt = journal.get(key + ":receipt")
        return key, snapshot(journal.bytes(key + ":receipt", receipt["raw_response_blob"]))
    return current["key"], {k: v for k, v in current.items() if k != "key"}


def status(journal, *, projection=None):
    saved = head(journal) if projection is None else projection
    result, current = journal.get("result"), saved["current"]
    value = dict(
        run_id=str(journal.run_id),
        approved=journal.get("approval") is not None,
        submissions=saved["submissions"],
        provider_job_id=current["id"] if current else None,
        usage=current.get("usage") if current else None,
        cost_usd=None,
        cost_basis="provider_invoice_not_available",
    )
    if saved.get("integrity_error"):
        value.update(state="needs_attention", operation_error=saved["integrity_error"])
    elif result:
        value.update(state="completed", result_ref=journal.ref("result").model_dump(mode="json"))
    elif current:
        value["state"] = (
            "result_processing_required" if current["status"] == "completed" else current["status"]
        )
    elif saved["submissions"]:
        value["state"] = "provider_rejected" if saved["submit_rejected"] else "outcome_unknown"
    else:
        value["state"] = "prepared"
    return value
