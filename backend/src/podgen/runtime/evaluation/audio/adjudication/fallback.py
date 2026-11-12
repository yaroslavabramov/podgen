"""One durable provider transition for all remaining windows in a /2 run."""

import os
from decimal import Decimal

from podgen.domain.errors import ContractError

from .attempts import exchange_state, history
from .contracts import prefix
from .processing import process
from .providers import settings
from .submission import submit


def switch_key(config):
    return prefix(config.review_id) + ":provider-switch"


def active_provider(journal, config):
    return "openai" if journal.get(switch_key(config)) else "gemini"


def superseded(journal, config, attempt_key):
    event = journal.get(switch_key(config))
    return bool(
        event
        and event["source_attempt_ref"] == journal.ref(attempt_key + ":prepared").model_dump(mode="json")
    )


def unavailable(row, current):
    if current == "outcome_unknown":
        return True
    receipt = row["receipt"]
    return (
        current == "provider_rejected"
        and receipt is not None
        and (receipt["status_code"] in (408, 429) or 500 <= receipt["status_code"] <= 599)
    )


def switch(journal, config, row, current):
    journal.put(
        switch_key(config),
        dict(
            contract="audio-review-provider-switch/1",
            from_provider="gemini",
            to_provider="openai",
            reason=current,
            policy=config.fallback.policy,
            source_attempt_ref=journal.ref(row["key"] + ":prepared").model_dump(mode="json"),
            source_submit_ref=journal.ref(row["key"] + ":submit").model_dump(mode="json"),
            source_receipt_ref=journal.ref(row["key"] + ":receipt").model_dump(mode="json")
            if row["receipt"]
            else None,
            previous_reservation_retained=True,
        ),
    )


def run_windows(journal, config, manifest, transport, ledger, retry_window, reason):
    if retry_window is not None and (
        not reason or not reason.strip() or not 1 <= retry_window <= len(manifest["windows"])
    ):
        raise ContractError("audio_review_retry_requires_window_and_reason")
    for number, window in enumerate(manifest["windows"], 1):
        if retry_window is not None and number != retry_window:
            continue
        rows = history(journal, window["key"])
        retry = retry_window is not None
        if retry and (
            not rows or exchange_state(journal, rows[-1]["key"]) in ("reviewed", "prepared", "receipt_saved")
        ):
            raise ContractError("audio_review_retry_requires_failed_or_unknown_request")
        while True:
            rows = history(journal, window["key"])
            current = exchange_state(journal, rows[-1]["key"]) if rows else "prepared"
            if current == "reviewed" and not superseded(journal, config, rows[-1]["key"]):
                break
            provider = active_provider(journal, config)
            previous_provider = rows[-1]["prepared"].get("provider") if rows else None
            # A persisted switch supersedes the primary attempt, even if a late receipt arrives.
            transitioned = bool(rows and previous_provider == "gemini" and provider == "openai")
            if current == "receipt_saved" and not transitioned and not retry:
                if process(journal, rows[-1]["key"], config) == "reviewed":
                    break
                return
            if current in ("provider_rejected", "outcome_unknown") and provider == "gemini":
                if unavailable(rows[-1], current):
                    switch(journal, config, rows[-1], current)
                    retry = False
                    continue
                return
            if current not in ("prepared", "receipt_saved") and not transitioned and not retry:
                return
            profile = settings(config, provider)
            credential = os.environ.get(profile.credential_env)
            if not credential:
                return
            count, cost = ledger(journal, manifest)
            if count >= config.max_requests or cost + Decimal(profile.reservation_per_request_usd) > Decimal(
                config.max_cost_usd
            ):
                return
            new_attempt = transitioned or retry
            key = f"{window['key']}:attempt:{len(rows) + 1}" if new_attempt or not rows else rows[-1]["key"]
            submit(
                journal,
                config,
                window,
                key,
                rows,
                "Availability fallback" if transitioned else reason,
                new_attempt,
                credential,
                transport,
                provider=provider,
            )
            retry = False
