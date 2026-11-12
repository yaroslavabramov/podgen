import hashlib
from pathlib import Path

from podgen.audio.fidelity.adjudication.contracts import WindowResolution
from podgen.audio.fidelity.adjudication.gemini import prepare as request_for
from podgen.audio.fidelity.adjudication.payload import VERSION as PAYLOAD_VERSION
from podgen.audio.fidelity.adjudication.payload import case_payload, window_segments
from podgen.audio.fidelity.adjudication.windows import group
from podgen.audio.fidelity.contracts import validate_report
from podgen.audio.media.recognition import clip
from podgen.domain.canonical import canonical_bytes, digest
from podgen.domain.errors import Conflict, ContractError

from ..fidelity.contracts import prefix as fidelity_prefix
from ..fidelity.processing import revisions
from ..fidelity.snapshots import schema_blob
from .contracts import ReviewConfigV2, ReviewConfigV3, prefix
from .local_resolution import resolve_zero
from .prepared_requests import fallback_request


def subject(journal, config):
    manifest = journal.get(fidelity_prefix(config.fidelity_audit_id) + ":manifest")
    if manifest is None or manifest["configuration"]["comparison_run_id"] != str(config.comparison_run_id):
        raise ContractError("review_fidelity_subject_not_found")
    for row in revisions(journal, config.fidelity_audit_id):
        if row["result"] and journal.ref(row["key"] + ":result") == config.candidate_result_ref:
            result = journal.base.read(config.candidate_result_ref)
            validate_report(result["report"])
            return manifest, result, journal.get(row["key"] + ":recognition")["output"]
    raise ContractError("review_candidates_outside_fidelity_audit")


def prepare(journal, config):
    key = prefix(config.review_id)
    raw = config.model_dump(mode="json")
    saved = journal.get(key + ":manifest")
    if saved:
        if saved["configuration_hash"] != digest(raw):
            raise Conflict("audio_review_configuration_changed")
        return saved
    manifest, result, recognition = subject(journal, config)
    prompt_bytes = Path(config.prompt_path).read_bytes()
    if hashlib.sha256(prompt_bytes).hexdigest() != config.prompt_sha256:
        raise ContractError("audio_review_prompt_hash_mismatch")
    cases = result["report"]["candidates"]
    if config.case_ids is not None:
        if not set(config.case_ids) <= {c["candidate_id"] for c in cases}:
            raise ContractError("audio_review_unknown_case_id")
        cases = [c for c in cases if c["candidate_id"] in config.case_ids]
    if any(not c.get("expected_context") for c in cases):
        raise ContractError("audio_review_requires_source_context_reprocess_candidates")
    proof = manifest["transport_proof"]
    rate = proof["sample_rate_hz"]
    grouped, excluded = group(cases, rate, config.max_window_seconds, config.max_cases_per_window)
    audio = journal.bytes(fidelity_prefix(config.fidelity_audit_id) + ":manifest", manifest["audio_blob"])
    aliases = {c["candidate_id"]: f"C{i}" for i, c in enumerate(cases, 1)}
    windows = []
    for index, window in enumerate(grouped, 1):
        window_key = f"{key}:window:{index}"
        clip_audio, extraction = clip(audio, window["start_frame"], window["end_frame"])
        audio_blob = journal.blob(clip_audio, "audio/wav")
        payload = dict(
            output_language=config.output_language,
            audio_window=dict(duration_seconds=(extraction["sample_frames"] * 1000 // rate) / 1000),
            asr_segment_excerpts=window_segments(
                recognition, window["start_frame"], extraction["sample_frames"], rate
            ),
            asr_segment_time_basis="seconds_relative_to_supplied_audio_window",
            cases=[
                case_payload(
                    c,
                    recognition,
                    manifest["source"],
                    window["start_frame"],
                    extraction["sample_frames"],
                    rate,
                    aliases[c["candidate_id"]],
                    source_tokens=result["report"]["expected_tokens"],
                    recognition_tokens=result["report"]["recognized_tokens"],
                )
                for c in window["cases"]
            ],
        )
        request, wire = request_for(prompt_bytes.decode("utf-8"), payload, clip_audio, config.reviewer)
        value = dict(
            contract="audio-review-window/1",
            case_ids=[aliases[c["candidate_id"]] for c in window["cases"]],
            case_aliases={aliases[c["candidate_id"]]: c["candidate_id"] for c in window["cases"]},
            source_candidate_result_ref=config.candidate_result_ref.model_dump(mode="json"),
            audio_blob=audio_blob,
            extraction=extraction,
            payload_contract=PAYLOAD_VERSION,
            payload_blob=journal.blob(canonical_bytes(payload), "application/json"),
            request=request,
            wire_blob=journal.blob(wire, "application/json"),
        )
        if isinstance(config, ReviewConfigV2):
            value.update(
                contract="audio-review-window/3"
                if isinstance(config, ReviewConfigV3)
                else "audio-review-window/2",
                providers=dict(
                    gemini=dict(request=request, wire_blob=value["wire_blob"]),
                    openai=fallback_request(
                        journal, config, prompt_bytes.decode("utf-8"), payload, clip_audio
                    ),
                ),
            )
        journal.put(window_key, value)
        if isinstance(config, ReviewConfigV3):
            resolve_zero(journal, window_key, value)
        windows.append(
            dict(
                key=window_key,
                ref=journal.ref(window_key).model_dump(mode="json"),
                case_ids=value["case_ids"],
            )
        )
    value = dict(
        contract="audio-review-manifest/3"
        if isinstance(config, ReviewConfigV3)
        else "audio-review-manifest/2"
        if isinstance(config, ReviewConfigV2)
        else "audio-review-manifest/1",
        configuration=raw,
        configuration_hash=digest(raw),
        source_candidate_result_ref=config.candidate_result_ref.model_dump(mode="json"),
        fidelity_manifest_ref=journal.ref(fidelity_prefix(config.fidelity_audit_id) + ":manifest").model_dump(
            mode="json"
        ),
        source_script_ref=manifest["source_script_ref"],
        source_script_hash=manifest["source_script_hash"],
        audio_variant=manifest["audio_variant"],
        source_audio_sha256=manifest["audio_blob"]["sha256"],
        playback_audio_sha256=manifest["playback_audio_blob"]["sha256"],
        prompt_blob=journal.blob(prompt_bytes, "text/plain"),
        prompt_sha256=config.prompt_sha256,
        config_schema_blob=schema_blob(journal, type(config)),
        response_schema_blob=schema_blob(journal, WindowResolution),
        windows=windows,
        case_aliases={alias: original for original, alias in aliases.items()},
        excluded_cases=excluded,
        expected_case_ids=[c["candidate_id"] for c in cases],
        payload_contract=PAYLOAD_VERSION,
        coverage_scope="supplied_candidate_windows_only",
        production_ready=False,
    )
    journal.put(key + ":manifest", value)
    return value
