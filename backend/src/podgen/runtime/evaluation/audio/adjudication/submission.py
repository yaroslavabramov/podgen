from datetime import datetime, timezone

from .contracts import prefix


def submit(journal, config, window, key, rows, reason, retry, credential, transport):
    if journal.get(key + ":prepared") is None:
        journal.put(
            key + ":prepared",
            dict(
                contract="audio-review-attempt/1",
                window_key=window["key"],
                window_ref=window["ref"],
                attempt=len(rows) + 1 if retry or not rows else rows[-1]["number"],
                reason=reason,
                parent_attempt_ref=journal.ref(rows[-1]["key"] + ":prepared").model_dump(mode="json")
                if rows and retry
                else None,
            ),
        )
    journal.put(
        key + ":submit",
        dict(
            contract="audio-review-submit-intent/1",
            manifest_ref=journal.ref(prefix(config.review_id) + ":manifest").model_dump(mode="json"),
            attempt_ref=journal.ref(key + ":prepared").model_dump(mode="json"),
            window_ref=window["ref"],
            submitted_at=datetime.now(timezone.utc).isoformat(),
            reservation_usd=config.reservation_per_request_usd,
        ),
    )
    request = journal.get(window["key"])
    response = transport.submit(
        request["request"], journal.bytes(window["key"], request["wire_blob"]), credential
    )
    journal.put(
        key + ":receipt",
        dict(
            contract="audio-review-http-receipt/1",
            submit_ref=journal.ref(key + ":submit").model_dump(mode="json"),
            status_code=response.status_code,
            headers=response.headers,
            raw_response_blob=journal.blob(
                response.body, response.headers.get("content-type", "application/octet-stream")
            ),
            elapsed_ms=response.elapsed_ms,
            complete=response.complete,
            error=response.error,
        ),
    )
