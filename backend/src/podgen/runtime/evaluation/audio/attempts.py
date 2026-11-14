"""Paid operations are explicit. A submit intent without a complete receipt is never sent again."""

import os
from datetime import datetime, timezone
from decimal import Decimal

from podgen.audio.synthesis.contracts import HttpReceipt, PreparedRequest
from podgen.audio.synthesis.transport import encode_request

from .contracts import VoiceCandidate
from .costs import charge, estimate
from .processing_history import runs


def stamp():
    return datetime.now(timezone.utc).isoformat()


def prefix(cell_id, take):
    return f"sample:{cell_id}:take:{take}"


def history(journal, cell):
    values = []
    take = 1
    while True:
        key = prefix(cell["cell_id"], take)
        record = journal.get(key + ":prepared")
        if record is None:
            break
        processing = runs(journal, key)
        values.append(
            dict(
                take=take,
                key=key,
                prepared=record,
                intent=journal.get(key + ":submit"),
                receipt=journal.get(key + ":receipt"),
                result=processing[-1]["result"],
                failure=processing[-1]["failure"],
                processing=processing,
                processing_key=processing[-1]["key"],
            )
        )
        take += 1
    return values


def ledger(journal, manifest):
    return OperationLedger(journal, manifest).totals(journal, manifest)


def _cell_ledger(journal, cell):
    attempts, committed = 0, Decimal(0)
    for row in history(journal, cell):
        if not row["intent"]:
            continue
        attempts += 1
        audio = journal.get(row["key"] + ":audio")
        if audio:
            cost = estimate(
                VoiceCandidate.model_validate(row["prepared"]["profile"]),
                audio["usage"],
                audio["usage_basis"],
            )
            committed += charge(cost)
        else:
            committed += Decimal(row["prepared"]["reservation_usd"])
    return attempts, committed


class OperationLedger:
    """Accounting under one run lock; no authorization facts survive a provider wait."""

    def __init__(self, journal, manifest):
        self.journal, self.manifest = journal, manifest
        self.cells = {cell["cell_id"]: _cell_ledger(journal, cell) for cell in manifest["cells"]}
        self.attempts = sum(row[0] for row in self.cells.values())
        self.committed = sum((row[1] for row in self.cells.values()), Decimal(0))

    def refresh(self, cell):
        before, after = self.cells[cell["cell_id"]], _cell_ledger(self.journal, cell)
        self.attempts += after[0] - before[0]
        self.committed += after[1] - before[1]
        self.cells[cell["cell_id"]] = after

    def totals(self, journal, manifest):
        if journal is not self.journal or manifest is not self.manifest:
            raise ValueError("audio_ledger_operation_mismatch")
        return self.attempts, self.committed


def execute(journal, manifest, cell, config, transport, *, retake_reason=None, operation_ledger=None):
    profile = next(p for p in config.profiles if p.profile_id == cell["profile_id"])
    previous = history(journal, cell)
    if retake_reason is None and previous:
        row = previous[-1]
        if row["result"]:
            return "completed"
        if row["failure"]:
            from .results import processing_contract

            latest = row["processing"][-1]
            if (
                row["receipt"]
                and latest["prepared"]
                and latest["prepared"]["processing_contract"] != processing_contract(config)
            ):
                from .reprocess import reprocess_prepared

                return reprocess_prepared(
                    journal,
                    config,
                    manifest,
                    cell["cell_id"],
                    "Resume local processing with "
                    + processing_contract(config)
                    + "; reuse the saved provider receipt.",
                )
            return row["failure"]["status"]
        if row["intent"] and row["receipt"] is None:
            return "outcome_unknown"
        take = row["take"]
    else:
        take = len(previous) + 1
    credential = os.environ.get(profile.credential_env)
    if not credential and not (previous and previous[-1]["receipt"] and retake_reason is None):
        journal.put(
            f"unavailable:{cell['cell_id']}",
            dict(
                contract="audio-unavailable/1",
                reason="credential_missing",
                credential_env=profile.credential_env,
            ),
        )
        return "not_configured"
    key = prefix(cell["cell_id"], take)
    if journal.get(key + ":prepared") is None:
        parent = journal.ref(previous[-1]["key"] + ":prepared").model_dump(mode="json") if previous else None
        journal.put(
            key + ":prepared",
            dict(
                contract="audio-attempt/1",
                cell_id=cell["cell_id"],
                take=take,
                request=cell["request"],
                wire_request_blob=journal.blob(
                    encode_request(PreparedRequest.model_validate(cell["request"])), "application/json"
                ),
                source_ref=cell["source_ref"],
                profile=profile.model_dump(mode="json"),
                reservation_usd=profile.reservation_usd,
                attempt_kind="artistic_retake" if retake_reason else "original",
                reason=retake_reason,
                parent_attempt_ref=parent,
            ),
        )
    receipt = journal.get(key + ":receipt")
    request = PreparedRequest.model_validate(cell["request"])
    if receipt is None:
        count, committed = (
            operation_ledger.totals(journal, manifest) if operation_ledger else ledger(journal, manifest)
        )
        if count >= config.max_attempts or committed + Decimal(profile.reservation_usd) > Decimal(
            config.max_cost_usd
        ):
            if journal.get(key + ":budget") is None:
                journal.put(
                    key + ":budget",
                    dict(
                        contract="audio-budget-block/1",
                        status="budget_exhausted",
                        attempts_submitted=count,
                        cost_or_reservation_usd=str(committed),
                        max_attempts=config.max_attempts,
                        max_cost_usd=config.max_cost_usd,
                    ),
                )
            return "budget_exhausted"
        if journal.get(key + ":submit"):
            return "outcome_unknown"
        from podgen.runtime.dispatch import before_external

        before_external("audio_submit", f"{journal.run_id}:{cell['cell_id']}:{take}")
        journal.put(
            key + ":submit",
            dict(
                contract="audio-submit-intent/1",
                prepared_ref=journal.ref(key + ":prepared").model_dump(mode="json"),
                submitted_at=stamp(),
            ),
        )
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
    response = HttpReceipt(
        receipt["status_code"],
        receipt["headers"],
        journal.bytes(key + ":receipt", receipt["raw_response_blob"]),
        receipt["elapsed_ms"],
        receipt["complete"],
        receipt["error"],
    )
    if not response.complete:
        status = "outcome_unknown"
        journal.put(
            key + ":failure",
            dict(
                contract="audio-failure/1",
                status=status,
                reason=response.error or "incomplete_http_response",
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            ),
        )
        return status
    if not 200 <= response.status_code < 300:
        journal.put(
            key + ":failure",
            dict(
                contract="audio-failure/1",
                status="provider_rejected",
                phase="provider",
                http_status=response.status_code,
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            ),
        )
        return "provider_rejected"
    from .results import process

    return process(journal, key, profile, config, request, response)


def baseline(rows):
    """Earliest usable synthesis take; later artistic takes never replace the comparison baseline."""
    for row in rows:
        successful = [revision for revision in row["processing"] if revision["result"]]
        if successful:
            selected = successful[-1]
            return dict(row, result=selected["result"], processing_key=selected["key"])
    return None
