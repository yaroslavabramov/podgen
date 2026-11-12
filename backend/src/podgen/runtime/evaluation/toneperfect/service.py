import hashlib
import os
from datetime import datetime, timezone

from podgen.audio.toneperfect import URL
from podgen.domain.errors import ContractError

from .observations import process, retryable
from .policy import guard, model_changed
from .prepare import prepare


def run(store, config, transport, *, case_id=None, retry_case=None, reason=None):
    with store.lock():
        manifest = prepare(store, config)
        ids = {case["case_id"] for case in manifest["cases"]}
        if (case_id and case_id not in ids) or (retry_case and retry_case not in ids):
            raise ContractError("toneperfect_unknown_case")
        if retry_case and (case_id or not reason or not reason.strip()):
            raise ContractError("toneperfect_retry_requires_reason")
        for case in manifest["cases"]:
            if (case_id or retry_case) and case["case_id"] != (case_id or retry_case):
                continue
            rows = store.attempts(case)
            if rows:
                result = process(store, case, rows[-1])
                if result:
                    if result["policy_violations"]:
                        return
                    if retry_case:
                        raise ContractError("toneperfect_cannot_repeat_completed_assessment")
                    continue
                if not retry_case:
                    return
                if not retryable(store, rows[-1]):
                    raise ContractError("toneperfect_request_not_retryable")
            elif retry_case:
                raise ContractError("toneperfect_retry_requires_existing_intent")
            if len(rows) >= config.max_attempts_per_case:
                raise ContractError("toneperfect_attempt_limit")
            histories = [(c, store.attempts(c)) for c in manifest["cases"]]
            guard(store, config, case, histories)
            credential = os.environ.get(config.credential_env, "")
            if not credential.startswith("tp_live_"):
                raise ContractError("toneperfect_live_key_required")
            request = case["request"]
            if request["url"] != URL:
                raise ContractError("toneperfect_unrecognized_origin")
            wire = store.bytes(case["wire_path"])
            if hashlib.sha256(wire).hexdigest() != request["wire_sha256"]:
                raise ContractError("toneperfect_frozen_wire_hash_mismatch")
            key = f"cases/{case['case_id']}/attempt-{len(rows) + 1}"
            store.put(
                key + "/submit.json",
                dict(
                    contract="toneperfect-submit-intent/1",
                    idempotency_key=case["idempotency_key"],
                    wire_sha256=request["wire_sha256"],
                    reservation_credits=case["reservation_credits"],
                    reason=reason,
                    parent_attempt=rows[-1]["key"] if rows else None,
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
            response = transport.submit(request, wire, credential)
            store.blob(key + "/response.bin", response.body)
            store.put(
                key + "/receipt.json",
                dict(
                    contract="toneperfect-http-receipt/1",
                    status_code=response.status_code,
                    headers=response.headers,
                    complete=response.complete,
                    error=response.error,
                    elapsed_ms=response.elapsed_ms,
                ),
            )
            result = process(store, case, store.attempts(case)[-1])
            if not result or result["policy_violations"]:
                return
            if model_changed(result, histories):
                store.put(key + "/model-change.json", dict(status="model_version_changed"))
                return
            if retry_case:
                return
