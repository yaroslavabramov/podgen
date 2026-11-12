import os
from decimal import Decimal
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from ..export_directory import bind_directory, write_json
from .attempts import history, state
from .contracts import ReviewConfigV2, prefix
from .fallback import active_provider, superseded, switch_key
from .providers import settings
from .service import ledger


def bind(journal, config, folder):
    key = prefix(config.review_id)
    manifest = journal.get(key + ":manifest")
    if manifest is None:
        raise ContractError("audio_review_not_prepared")
    bind_directory(
        folder,
        "review-binding.json",
        dict(
            contract="audio-review-export-binding/1",
            review_id=str(config.review_id),
            manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
            manifest_hash=digest(manifest),
        ),
        manifest,
    )
    return manifest


def export(journal, config, folder):
    folder = Path(folder)
    manifest = bind(journal, config, folder)
    key = prefix(config.review_id)
    write_json(folder / "private/manifest.json", manifest)
    for field, filename in (
        ("prompt_blob", "prompt.md"),
        ("config_schema_blob", "config.schema.json"),
        ("response_schema_blob", "response.schema.json"),
    ):
        (folder / "private" / filename).write_bytes(journal.bytes(key + ":manifest", manifest[field]))
    decisions, windows = [], []
    count, current_cost = ledger(journal, manifest)
    provider_halt = None
    provider = active_provider(journal, config) if isinstance(config, ReviewConfigV2) else "gemini"
    profile = settings(config, provider)
    event = journal.get(switch_key(config)) if isinstance(config, ReviewConfigV2) else None
    if isinstance(config, ReviewConfigV2):
        write_json(folder / "private/provider-switch.json", event)
    for index, ref in enumerate(manifest["windows"], 1):
        where = folder / "private" / f"window-{index}"
        window = journal.get(ref["key"])
        write_json(where / "window.json", window)
        for field, filename in (
            ("audio_blob", "audio.wav"),
            ("payload_blob", "payload.json"),
            ("wire_blob", "wire-request.json"),
        ):
            (where / filename).write_bytes(journal.bytes(ref["key"], window[field]))
        for name, request in window.get("providers", {}).items():
            (where / f"{name}-wire-request.json").write_bytes(journal.bytes(ref["key"], request["wire_blob"]))
        attempts = history(journal, ref["key"])
        write_json(where / "attempts.json", attempts)
        for attempt in attempts:
            place = where / f"attempt-{attempt['number']}"
            write_json(place / "prepared.json", attempt["prepared"])
            write_json(place / "submit.json", attempt["intent"])
            write_json(place / "receipt.json", attempt["receipt"])
            if attempt["receipt"]:
                (place / "raw-http-response.bin").write_bytes(
                    journal.bytes(attempt["key"] + ":receipt", attempt["receipt"]["raw_response_blob"])
                )
        replaced = bool(
            attempts
            and isinstance(config, ReviewConfigV2)
            and superseded(journal, config, attempts[-1]["key"])
        )
        processed = attempts[-1]["processing"] if attempts and not replaced else []
        if processed and processed[-1]["result"]:
            for decision in processed[-1]["result"]["decisions"]:
                decisions.append(
                    dict(
                        decision,
                        window_ref=ref["ref"],
                        result_ref=journal.ref(processed[-1]["key"] + ":result").model_dump(mode="json"),
                        source_start_frame=window["extraction"]["start_frame"],
                        sample_rate_hz=window["extraction"]["sample_rate_hz"],
                    )
                )
        current_state = "prepared" if replaced else state(journal, ref["key"])
        blocked_reason = None
        if current_state == "prepared":
            if provider_halt:
                blocked_reason = "stopped_after_provider_failure"
            elif count >= config.max_requests or current_cost + Decimal(
                profile.reservation_per_request_usd
            ) > Decimal(config.max_cost_usd):
                blocked_reason = "budget_exhausted"
            elif not os.environ.get(profile.credential_env):
                blocked_reason = "credential_missing"
        elif current_state in ("provider_rejected", "outcome_unknown", "response_invalid"):
            provider_halt = dict(window_ref=ref["ref"], status=current_state)
        windows.append(
            dict(
                window_ref=ref["ref"],
                case_ids=ref["case_ids"],
                status=current_state,
                blocked_reason=blocked_reason,
            )
        )
    counts = {
        name: sum(d["status"] == name for d in decisions)
        for name in ("confirmed", "dismissed", "inconclusive")
    }
    complete = len(decisions) == len(manifest["expected_case_ids"])
    outcome = "no_candidate_windows" if not manifest["expected_case_ids"] else "needs_attention"
    if manifest["expected_case_ids"] and complete and not counts["inconclusive"]:
        outcome = "confirmed_defects" if counts["confirmed"] else "candidates_dismissed"
    submitted, cost = ledger(journal, manifest)
    report = dict(
        contract="audio-candidate-adjudication-report/1",
        manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
        decisions=decisions,
        not_submitted_cases=manifest["excluded_cases"],
        windows=windows,
        coverage_scope=manifest["coverage_scope"],
        audio_variant=manifest["audio_variant"],
        whole_recording_review="not_performed",
        production_ready=False,
    )
    write_json(folder / "decisions.json", report)
    status = dict(
        contract="audio-review-export/1",
        status=outcome,
        prepared_windows=len(manifest["windows"]),
        expected_cases=len(manifest["expected_case_ids"]),
        resolved_cases=len(decisions),
        counts=counts,
        all_cases_resolved=complete,
        requests_submitted=submitted,
        cost_or_reservation_usd=str(cost),
        required_reservation_usd=str(Decimal(profile.reservation_per_request_usd) * len(manifest["windows"])),
        provider_halt=provider_halt,
        coverage_scope=manifest["coverage_scope"],
        whole_recording_review="not_performed",
        production_ready=False,
        **(
            dict(active_provider=provider, provider_switch=event)
            if isinstance(config, ReviewConfigV2)
            else {}
        ),
    )
    write_json(folder / "status.json", status)
    return status
