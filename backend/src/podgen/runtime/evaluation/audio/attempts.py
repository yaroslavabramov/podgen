"""Execute one exact synthesis attempt; paid receipts and results stay with this operation."""

import os
from datetime import datetime, timezone
from decimal import Decimal

from podgen.audio.synthesis.contracts import HttpReceipt, PreparedRequest
from podgen.audio.synthesis.transport import encode_request

from .accounting import OperationLedger
from .accounting import ledger as ledger
from .history_snapshot import load


def stamp():
    return datetime.now(timezone.utc).isoformat()


def prefix(cell_id, take):
    return f"sample:{cell_id}:take:{take}"


def history(journal, cell):
    return load(journal, [cell])[cell["cell_id"]]


def execute(journal, manifest, cell, config, transport, **options):
    """Compatibility status API; production owners consume execute_sample's descriptor."""
    return execute_sample(journal, manifest, cell, config, transport, **options).status


def execute_sample(journal, manifest, cell, config, transport, *, retake_reason=None, operation_ledger=None):
    operation = operation_ledger or OperationLedger(journal, manifest)
    operation.totals(journal, manifest)
    try:
        status = _execute(journal, manifest, cell, config, transport, operation, retake_reason)
    except BaseException:
        # SQL/object publication may have committed before its acknowledgement
        # was lost. Never trust this cell's in-memory state after an exception.
        operation.invalidate(cell)
        raise
    finally:
        # Numeric accounting and immutable results are updated from the work just
        # performed, including a submit whose acknowledgement was lost.
        if not operation.is_dirty(cell):
            operation.update(cell)
    return operation.result(cell, status)


def _processed(row, processed):
    revision = dict(
        key=processed.key,
        prepared=processed.prepared,
        result=processed.result,
        failure=processed.failure,
        decoded=processed.decoded,
    )
    if row["processing"] and row["processing"][-1]["key"] == processed.key:
        row["processing"][-1] = revision
    else:
        row["processing"].append(revision)
    row.update(result=processed.result, failure=processed.failure, processing_key=processed.key)
    if processed.audio is not None:
        row["audio"] = processed.audio
    return processed.status


def _execute(journal, manifest, cell, config, transport, operation, retake_reason):
    from .results import process_result, processing_contract

    profile = next(p for p in config.profiles if p.profile_id == cell["profile_id"])
    previous = operation.rows[cell["cell_id"]]
    row = previous[-1] if previous and retake_reason is None else None
    if row:
        if row["result"]:
            return "completed"
        if row["failure"]:
            latest = row["processing"][-1]
            if (
                row["receipt"]
                and latest["prepared"]
                and latest["prepared"]["processing_contract"] != processing_contract(config)
            ):
                from .reprocess import reprocess_attempt

                return _processed(
                    row,
                    reprocess_attempt(
                        journal,
                        config,
                        cell,
                        row,
                        "Resume local processing with "
                        + processing_contract(config)
                        + "; reuse the saved provider receipt.",
                    ),
                )
            return row["failure"]["status"]
        if row["intent"] and row["receipt"] is None:
            return "outcome_unknown"
    credential = os.environ.get(profile.credential_env)
    if not credential and not (row and row["receipt"]):
        journal.put(
            f"unavailable:{cell['cell_id']}",
            dict(
                contract="audio-unavailable/1",
                reason="credential_missing",
                credential_env=profile.credential_env,
            ),
        )
        return "not_configured"
    request = PreparedRequest.model_validate(cell["request"])
    if row is None:
        take = previous[-1]["take"] + 1 if previous else 1
        key = prefix(cell["cell_id"], take)
        prepared = dict(
            contract="audio-attempt/1",
            cell_id=cell["cell_id"],
            take=take,
            request=cell["request"],
            wire_request_blob=journal.blob(encode_request(request), "application/json"),
            source_ref=cell["source_ref"],
            profile=profile.model_dump(mode="json"),
            reservation_usd=profile.reservation_usd,
            attempt_kind="artistic_retake" if retake_reason else "original",
            reason=retake_reason,
            parent_attempt_ref=journal.ref(previous[-1]["key"] + ":prepared").model_dump(mode="json")
            if previous
            else None,
        )
        journal.put(key + ":prepared", prepared)
        row = dict(
            take=take,
            key=key,
            prepared=prepared,
            intent=None,
            receipt=None,
            audio=None,
            budget=None,
            result=None,
            failure=None,
            processing=[dict(key=key, prepared=None, result=None, failure=None, decoded=None)],
            processing_key=key,
        )
        previous.append(row)
    key, receipt = row["key"], row["receipt"]
    if receipt is None:
        count, committed = operation.totals(journal, manifest)
        if count >= config.max_attempts or committed + Decimal(profile.reservation_usd) > Decimal(
            config.max_cost_usd
        ):
            budget = dict(
                contract="audio-budget-block/1",
                status="budget_exhausted",
                attempts_submitted=count,
                cost_or_reservation_usd=str(committed),
                max_attempts=config.max_attempts,
                max_cost_usd=config.max_cost_usd,
            )
            if row["budget"] is None:
                journal.put(key + ":budget", budget)
                row["budget"] = budget
            return "budget_exhausted"
        from podgen.runtime.dispatch import before_external

        before_external("audio_submit", f"{journal.run_id}:{cell['cell_id']}:{row['take']}")
        intent = dict(
            contract="audio-submit-intent/1",
            prepared_ref=journal.ref(key + ":prepared").model_dump(mode="json"),
            submitted_at=stamp(),
        )
        journal.put(key + ":submit", intent)
        row["intent"] = intent
        response = transport.submit(request, credential)
        receipt = dict(
            contract="audio-http-receipt/1",
            prepared_ref=journal.ref(key + ":prepared").model_dump(mode="json"),
            status_code=response.status_code,
            headers=response.headers,
            raw_response_blob=journal.blob(
                response.body, response.headers.get("content-type", "application/octet-stream")
            ),
            elapsed_ms=response.elapsed_ms,
            complete=response.complete,
            error=response.error,
            received_at=stamp(),
        )
        journal.put(key + ":receipt", receipt)
        row["receipt"] = receipt
    else:
        response = HttpReceipt(
            receipt["status_code"],
            receipt["headers"],
            journal.bytes(key + ":receipt", receipt["raw_response_blob"]),
            receipt["elapsed_ms"],
            receipt["complete"],
            receipt["error"],
        )
    failure = None
    if not response.complete:
        failure = dict(
            contract="audio-failure/1",
            status="outcome_unknown",
            reason=response.error or "incomplete_http_response",
            receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
        )
    elif not 200 <= response.status_code < 300:
        failure = dict(
            contract="audio-failure/1",
            status="provider_rejected",
            phase="provider",
            http_status=response.status_code,
            receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
        )
    if failure:
        journal.put(key + ":failure", failure)
        row["failure"] = failure
        row["processing"][-1]["failure"] = failure
        return failure["status"]
    return _processed(
        row,
        process_result(
            journal,
            key,
            profile,
            config,
            request,
            response,
            output_key=row["processing_key"],
            attempt=row["prepared"],
            processing=row["processing"][-1]["prepared"],
        ),
    )


def usable(row):
    """This take with its latest successful processing, or None."""
    successful = [revision for revision in row["processing"] if revision["result"]]
    if successful:
        selected = successful[-1]
        return dict(row, result=selected["result"], processing_key=selected["key"])
    return None


def baseline(rows):
    """Earliest usable take; explicit artistic retakes never replace this baseline."""
    return next((selected for selected in map(usable, rows) if selected), None)
