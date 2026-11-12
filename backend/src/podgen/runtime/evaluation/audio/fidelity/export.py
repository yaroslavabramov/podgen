from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from ..export_directory import bind_directory, write_json
from ..immutable_export import write_bytes as immutable_bytes
from ..immutable_export import write_json as immutable_json
from .contracts import prefix
from .deliveries import deliveries, latest, reserved
from .export_results import export_results
from .processing import revisions
from .service import state


def bind(journal, config, folder):
    key = prefix(config.audit_id)
    manifest = journal.get(key + ":manifest")
    if manifest is None:
        raise ContractError("fidelity_not_prepared")
    bind_directory(
        folder,
        "fidelity-binding.json",
        dict(
            contract="audio-fidelity-export-binding/1",
            audit_id=str(config.audit_id),
            manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
            manifest_hash=digest(manifest),
        ),
        manifest,
    )
    return manifest


def export(journal, config, folder):
    folder = Path(folder)
    manifest = bind(journal, config, folder)
    key = prefix(config.audit_id)
    immutable_json(folder / "private/manifest.json", manifest)
    for name in ("config", "recognition", "candidates"):
        blob = manifest[name + "_schema_blob"]
        immutable_bytes(folder / "private" / (name + ".schema.json"), journal.bytes(key + ":manifest", blob))
    immutable_bytes(
        folder / "private/asr-input.flac", journal.bytes(key + ":manifest", manifest["transport_blob"])
    )
    immutable_bytes(
        folder / "private/wire-request.bin", journal.bytes(key + ":manifest", manifest["wire_blob"])
    )
    delivery = latest(journal, config)
    delivery_key = delivery["key"]
    intent, receipt = delivery["intent"], delivery["receipt"]
    all_deliveries = deliveries(journal, config)
    write_json(folder / "private/deliveries.json", all_deliveries)
    for number, row in enumerate(all_deliveries, 1):
        place = folder / "private" / f"delivery-{number}"
        write_json(place / "delivery.json", row)
        if row["receipt"]:
            immutable_bytes(
                place / "raw-http-response.bin",
                journal.bytes(row["key"] + ":receipt", row["receipt"]["raw_response_blob"]),
            )
    write_json(folder / "private/submit.json", intent)
    write_json(folder / "private/receipt.json", receipt)
    if receipt:
        (folder / "private/raw-http-response.bin").write_bytes(
            journal.bytes(delivery_key + ":receipt", receipt["raw_response_blob"])
        )
    processed = revisions(journal, config.audit_id)
    last_success = None
    for i, revision in enumerate(processed, 1):
        place = folder / "private" / f"processing-{i}"
        write_json(place / "processing.json", revision)
        for name in ("recognition", "candidates"):
            blob = revision["prepared"].get(name + "_schema_blob")
            if blob:
                (place / (name + ".schema.json")).write_bytes(journal.bytes(revision["key"], blob))
        recognition = journal.get(revision["key"] + ":recognition")
        write_json(place / "recognition.json", recognition)
        if revision["result"]:
            last_success = revision["result"]
            for candidate in last_success["report"]["candidates"]:
                if candidate.get("clip_blob"):
                    immutable_bytes(
                        place / (candidate["candidate_id"] + ".wav"),
                        journal.bytes(revision["key"] + ":result", candidate["clip_blob"]),
                    )
    pointer, active = export_results(journal, config, folder)
    count, cost = reserved(journal, config)
    status = dict(
        contract="audio-fidelity-export/1",
        status=state(journal, config),
        manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
        requests_submitted=count,
        max_requests=config.max_requests,
        current=pointer,
        authorized_max_requests=delivery["authorization"]["policy"]["max_total_requests"]
        if delivery["authorization"]
        else config.max_requests,
        cost_or_reservation_usd=str(cost),
        cost_status="reservations_not_invoice",
        candidates=len(active["result"]["report"]["candidates"]) if active and active["result"] else 0,
        checked_audio_variant=manifest["audio_variant"],
        checked_audio_sha256=manifest["audio_blob"]["sha256"],
        playback_audio_sha256=manifest["playback_audio_blob"]["sha256"],
        audio_review_status="pending",
        production_ready=False,
    )
    write_json(folder / "status.json", status)
    return status
