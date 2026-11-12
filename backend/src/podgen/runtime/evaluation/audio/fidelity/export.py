from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from ..export_directory import bind_directory, write_json
from .contracts import prefix
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
    write_json(folder / "private/manifest.json", manifest)
    for name in ("config", "recognition", "candidates"):
        blob = manifest[name + "_schema_blob"]
        (folder / "private" / (name + ".schema.json")).write_bytes(journal.bytes(key + ":manifest", blob))
    (folder / "private/asr-input.flac").write_bytes(
        journal.bytes(key + ":manifest", manifest["transport_blob"])
    )
    (folder / "private/wire-request.bin").write_bytes(journal.bytes(key + ":manifest", manifest["wire_blob"]))
    intent, receipt = journal.get(key + ":submit"), journal.get(key + ":receipt")
    write_json(folder / "private/submit.json", intent)
    write_json(folder / "private/receipt.json", receipt)
    if receipt:
        (folder / "private/raw-http-response.bin").write_bytes(
            journal.bytes(key + ":receipt", receipt["raw_response_blob"])
        )
    processed = revisions(journal, config.audit_id)
    latest = None
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
            latest = revision["result"]
            for candidate in latest["report"]["candidates"]:
                if candidate.get("clip_blob"):
                    (place / (candidate["candidate_id"] + ".wav")).write_bytes(
                        journal.bytes(revision["key"] + ":result", candidate["clip_blob"])
                    )
    if latest:
        write_json(folder / "candidates.json", latest)
    status = dict(
        contract="audio-fidelity-export/1",
        status=state(journal, config),
        manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
        requests_submitted=int(bool(intent)),
        max_requests=config.max_requests,
        cost_or_reservation_usd=(manifest["cost"]["usd"] if latest else config.reservation_usd)
        if intent
        else "0",
        cost_status="submitted_audio_duration_estimate" if latest else "reservation_not_invoice",
        candidates=len(latest["report"]["candidates"]) if latest else 0,
        checked_audio_variant=manifest["audio_variant"],
        checked_audio_sha256=manifest["audio_blob"]["sha256"],
        playback_audio_sha256=manifest["playback_audio_blob"]["sha256"],
        audio_review_status="pending",
        production_ready=False,
    )
    write_json(folder / "status.json", status)
    return status
