from datetime import datetime, timezone

from .contracts import prefix
from .providers import request_for, settings
from .receipts import fields, media_type


def submit(journal, config, window, key, rows, reason, retry, credential, transport, provider=None):
    profile = settings(config, provider)
    provenance = dict(provider=provider) if provider else {}
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
                **provenance,
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
            reservation_usd=profile.reservation_per_request_usd,
            **provenance,
        ),
    )
    request = request_for(journal.get(window["key"]), provider)
    response = transport.submit(
        request["request"], journal.bytes(window["key"], request["wire_blob"]), credential
    )
    journal.put(
        key + ":receipt",
        dict(
            **fields(response),
            submit_ref=journal.ref(key + ":submit").model_dump(mode="json"),
            raw_response_blob=journal.blob(response.body, media_type(response)),
            elapsed_ms=response.elapsed_ms,
            complete=response.complete,
            error=response.error,
            **provenance,
        ),
    )
