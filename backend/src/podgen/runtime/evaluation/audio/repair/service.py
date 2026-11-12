import os
from decimal import Decimal

from podgen.domain.errors import ContractError

from ..exchange_history import attempts
from ..subjects import synthesis_part
from .attempts import ledger, state
from .contracts import prefix
from .dispatch import record as dispatch_record
from .evidence import verify
from .prepare import prepare
from .processing import process
from .submission import submit
from .verification import prepare_verification


def run(journal, config, transport, *, retry_reason=None):
    root = prefix(config.repair_id)
    with journal.lock():
        manifest = prepare(journal, config)
        rows = attempts(journal, root)
        if retry_reason is not None:
            if (
                not retry_reason.strip()
                or not rows
                or state(journal, rows[-1]["key"]) in ("prepared", "receipt_saved", "part_generated")
            ):
                raise ContractError("repair_retry_requires_failed_attempt_and_reason")
            key = f"{root}:attempt:{len(rows) + 1}"
        else:
            key = rows[-1]["key"] if rows else root + ":attempt:1"
        current = state(journal, key)
        if current == "prepared":
            credential = os.environ.get(manifest["profile"]["credential_env"])
            if not credential:
                return dispatch_record(journal, config, "credential_missing")
            count, cost = ledger(journal, root)
            if count >= config.max_requests or cost + Decimal(config.reservation_per_request_usd) > Decimal(
                config.max_cost_usd
            ):
                return dispatch_record(journal, config, "budget_exhausted")
            verify(journal, config, synthesis_part(journal, config.part_source))
            submit(journal, config, manifest, key, rows, credential, transport, retry_reason)
            current = state(journal, key)
        if current == "receipt_saved":
            current = process(journal, config, manifest, key)
        if current != "part_generated":
            return dispatch_record(journal, config, current)
        latest = attempts(journal, root)[-1]["processing"][-1]
        result_ref = journal.ref(latest["key"] + ":result")
    return finish(journal, config, result_ref)


def reprocess(journal, config, reason):
    if not reason or not reason.strip():
        raise ContractError("repair_reprocess_reason_required")
    root = prefix(config.repair_id)
    with journal.lock():
        manifest = prepare(journal, config)
        rows = attempts(journal, root)
        if not rows or not rows[-1]["receipt"]:
            raise ContractError("repair_reprocess_requires_receipt")
        row = rows[-1]
        receipt = row["receipt"]
        if not receipt["complete"] or receipt["error"] or not 200 <= receipt["status_code"] < 300:
            raise ContractError("repair_reprocess_requires_complete_success_receipt")
        current = process(journal, config, manifest, row["key"], reason)
        if current != "part_generated":
            return dispatch_record(journal, config, current)
        latest = attempts(journal, root)[-1]["processing"][-1]
        result_ref = journal.ref(latest["key"] + ":result")
    return finish(journal, config, result_ref)


def finish(journal, config, result_ref):
    try:
        prepare_verification(journal, config, result_ref)
    except ContractError as error:
        with journal.lock():
            dispatch_record(
                journal, config, "verification_preparation_failed", result_ref=result_ref, reason=error.code
            )
        raise
    with journal.lock():
        return dispatch_record(journal, config, "verification_required", result_ref=result_ref)
