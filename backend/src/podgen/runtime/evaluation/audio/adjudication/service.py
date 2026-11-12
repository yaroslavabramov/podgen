import os
from decimal import Decimal

from podgen.domain.errors import ContractError

from .attempts import exchange_state, history
from .prepare import prepare
from .processing import process
from .submission import submit


def ledger(journal, manifest):
    count, cost = 0, Decimal(0)
    for window in manifest["windows"]:
        for attempt in history(journal, window["key"]):
            if not attempt["intent"]:
                continue
            count += 1
            successful = [r["result"] for r in attempt["processing"] if r["result"]]
            cost += Decimal(
                successful[-1]["cost"]["usd"] if successful else attempt["intent"]["reservation_usd"]
            )
    return count, cost


def run(journal, config, transport, *, retry_window=None, reason=None):
    with journal.lock():
        manifest = prepare(journal, config)
        if retry_window is not None and (
            not reason or not reason.strip() or not 1 <= retry_window <= len(manifest["windows"])
        ):
            raise ContractError("audio_review_retry_requires_window_and_reason")
        for number, window in enumerate(manifest["windows"], 1):
            if retry_window is not None and number != retry_window:
                continue
            rows = history(journal, window["key"])
            if retry_window is not None:
                if not rows or exchange_state(journal, rows[-1]["key"]) in (
                    "reviewed",
                    "prepared",
                    "receipt_saved",
                ):
                    raise ContractError("audio_review_retry_requires_failed_or_unknown_request")
                key = f"{window['key']}:attempt:{len(rows) + 1}"
                current = "prepared"
            else:
                key = rows[-1]["key"] if rows else window["key"] + ":attempt:1"
                current = exchange_state(journal, key)
            if current == "reviewed":
                continue
            if current not in ("prepared", "receipt_saved"):
                break
            if current == "prepared":
                credential = os.environ.get(config.credential_env)
                if not credential:
                    break
                count, cost = ledger(journal, manifest)
                if count >= config.max_requests or cost + Decimal(
                    config.reservation_per_request_usd
                ) > Decimal(config.max_cost_usd):
                    break
                submit(
                    journal,
                    config,
                    window,
                    key,
                    rows,
                    reason,
                    retry_window is not None,
                    credential,
                    transport,
                )
                current = exchange_state(journal, key)
            if current != "receipt_saved" or process(journal, key, config) != "reviewed":
                break


def reprocess(journal, config, reason):
    if not reason or not reason.strip():
        raise ContractError("audio_review_reprocess_reason_required")
    with journal.lock():
        manifest = prepare(journal, config)
        for window in manifest["windows"]:
            for row in history(journal, window["key"]):
                receipt = row["receipt"]
                if (
                    receipt
                    and receipt["complete"]
                    and not receipt["error"]
                    and 200 <= receipt["status_code"] < 300
                ):
                    process(journal, row["key"], config, reason)
