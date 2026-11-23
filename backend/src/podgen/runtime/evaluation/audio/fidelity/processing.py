from pydantic import ValidationError

from podgen.audio.fidelity.alignment import provenance
from podgen.audio.fidelity.candidates import VERSION_CANDIDATES, generate
from podgen.audio.fidelity.contracts import CandidateReportV7
from podgen.audio.fidelity.tokens import VERSION as NORMALIZATION
from podgen.audio.media.recognition import clip
from podgen.audio.recognition.contracts import VERSION, Recognition
from podgen.audio.recognition.fish import parse
from podgen.domain.errors import ContractError

from .contracts import prefix
from .deliveries import latest
from .snapshots import schema_blob


def revisions(journal, audit_id):
    values, number = [], 1
    while journal.get(f"{prefix(audit_id)}:processing:{number}") is not None:
        key = f"{prefix(audit_id)}:processing:{number}"
        values.append(
            dict(
                key=key,
                prepared=journal.get(key),
                result=journal.get(key + ":result"),
                failure=journal.get(key + ":failure"),
            )
        )
        number += 1
    return values


def process(journal, config, manifest, *, reason=None):
    key = prefix(config.audit_id)
    receipt_key = latest(journal, config)["key"]
    previous = revisions(journal, config.audit_id)
    output_key = f"{key}:processing:{len(previous) + 1}"
    journal.put(
        output_key,
        dict(
            contract="audio-fidelity-processing/1",
            source_receipt_ref=journal.ref(receipt_key + ":receipt").model_dump(mode="json"),
            parent_processing_ref=journal.ref(previous[-1]["key"]).model_dump(mode="json")
            if previous
            else None,
            reason=reason,
            recognition_contract=VERSION,
            candidates_contract=VERSION_CANDIDATES,
            normalization_contract=NORMALIZATION,
            alignment=provenance(),
            recognition_schema_blob=schema_blob(journal, Recognition),
            candidates_schema_blob=schema_blob(journal, CandidateReportV7),
        ),
    )
    try:
        receipt = journal.get(receipt_key + ":receipt")
        parsed = parse(journal.bytes(receipt_key + ":receipt", receipt["raw_response_blob"]))
        journal.put(
            output_key + ":recognition",
            dict(
                contract="audio-independent-recognition/1",
                output=parsed.model_dump(mode="json"),
                receipt_ref=journal.ref(receipt_key + ":receipt").model_dump(mode="json"),
                submitted_audio_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
                submitted_audio_sha256=manifest["transport_proof"]["source_audio_sha256"],
                submitted_range=manifest["transport_proof"]["submitted_range"],
            ),
        )
        proof = manifest["transport_proof"]
        report = generate(
            config.audit_id,
            manifest["source"]["source"]["utterances"],
            parsed,
            proof["sample_rate_hz"],
            proof["sample_frames"],
            margin_seconds=config.clip_margin_seconds,
            max_clip_seconds=config.max_review_clip_seconds,
        )
        audio = journal.bytes(key + ":manifest", manifest["audio_blob"])
        for candidate in report["candidates"]:
            if candidate["clip_status"] != "ready_for_extraction":
                continue
            selected = candidate["audio_range"]
            body, extraction = clip(audio, selected["start_frame"], selected["end_frame"])
            candidate.update(
                clip_blob=journal.blob(body, "audio/wav"), extraction=extraction, clip_status="extracted"
            )
        CandidateReportV7.model_validate(report)
        journal.put(
            output_key + ":result",
            dict(
                contract="audio-fidelity-candidate-result/1",
                recognition_ref=journal.ref(output_key + ":recognition").model_dump(mode="json"),
                subject_manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
                report=report,
                checked_audio=dict(
                    variant=manifest["audio_variant"],
                    subject_ref=manifest.get("subject_ref", manifest.get("subject_result_ref")),
                    subject_kind=manifest.get("subject_kind", "comparison_result"),
                    audio_sha256=manifest["audio_blob"]["sha256"],
                    decoded_ref=manifest["decoded_ref"],
                    playback_audio_sha256=manifest["playback_audio_blob"]["sha256"],
                    playback_normalization=manifest["playback_normalization"],
                ),
                audio_review_status="pending",
                production_ready=False,
            ),
        )
        return "candidates_generated"
    except (ValueError, KeyError, TypeError, ValidationError, ContractError) as error:
        journal.put(
            output_key + ":failure",
            dict(
                contract="audio-fidelity-processing-failure/1",
                status="response_invalid",
                reason=str(error),
                receipt_ref=journal.ref(receipt_key + ":receipt").model_dump(mode="json"),
            ),
        )
        return "response_invalid"
