from decimal import Decimal

from podgen.audio.fidelity.candidates import VERSION_CANDIDATES
from podgen.audio.fidelity.contracts import CandidateReport
from podgen.audio.media.processing import tools
from podgen.audio.media.recognition import lossless_flac
from podgen.audio.recognition.contracts import VERSION, Recognition
from podgen.audio.recognition.fish import prepare as prepare_request
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import Conflict, ContractError

from .contracts import prefix
from .snapshots import schema_blob
from .subject import resolve


def prepare(journal, config):
    if config.comparison_run_id != journal.run_id:
        raise ContractError("fidelity_wrong_comparison_run")
    key = prefix(config.audit_id)
    raw = config.model_dump(mode="json")
    saved = journal.get(key + ":manifest")
    if saved:
        if saved["configuration_hash"] != digest(raw):
            raise Conflict("fidelity_configuration_changed")
        return saved
    subject = resolve(journal, config.subject_ref, kind=config.subject_kind)
    audio = journal.base.read_blob(config.subject_ref, BlobRef.model_validate(subject["audio_blob"]))
    transport, proof = lossless_flac(audio)
    duration = Decimal(proof["sample_frames"]) / Decimal(proof["sample_rate_hz"])
    estimated = duration / 60 * Decimal(config.price.usd_per_audio_minute)
    if estimated > Decimal(config.reservation_usd):
        raise ContractError("recognition_reservation_below_submitted_audio_estimate")
    request, wire = prepare_request(config.recognition, transport)
    value = dict(
        contract="audio-fidelity-manifest/1",
        configuration=raw,
        configuration_hash=digest(raw),
        comparison_manifest_ref=journal.ref("manifest").model_dump(mode="json"),
        subject_ref=config.subject_ref.model_dump(mode="json"),
        subject_kind=config.subject_kind,
        source_ref=subject["source_ref"],
        source_script_ref=subject["source"]["source_script_ref"],
        source_script_hash=subject["source"]["source_script_hash"],
        source=subject["source"],
        decoded_ref=subject.get("decoded_ref"),
        audio_blob=subject["audio_blob"],
        audio_variant=subject["audio_variant"],
        playback_audio_blob=subject["playback_audio_blob"],
        playback_normalization=subject["playback_normalization"],
        transport_blob=journal.blob(transport, "audio/flac"),
        transport_proof=proof,
        request=request,
        wire_blob=journal.blob(wire, "multipart/form-data"),
        prompt_snapshot=None,
        expected_text_sent_to_recognizer=False,
        config_schema_blob=schema_blob(journal, type(config)),
        recognition_schema_blob=schema_blob(journal, Recognition),
        candidates_schema_blob=schema_blob(journal, CandidateReport),
        recognition_contract=VERSION,
        candidates_contract=VERSION_CANDIDATES,
        tool_versions=tools(),
        cost=dict(
            status="submitted_audio_duration_estimate",
            usd=str(estimated),
            price=config.price.model_dump(mode="json"),
        ),
        production_ready=False,
    )
    value["subject_result_ref" if config.subject_kind == "comparison_result" else "subject_assembly_ref"] = (
        config.subject_ref.model_dump(mode="json")
    )
    journal.put(key + ":manifest", value)
    return value
