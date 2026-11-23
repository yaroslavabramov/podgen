from podgen.audio.media.processing import tools
from podgen.audio.media.recognition import clip
from podgen.audio.synthesis.registry import adapter
from podgen.audio.synthesis.transport import encode_request
from podgen.domain.canonical import canonical_bytes, digest
from podgen.domain.errors import Conflict, ContractError

from ..contracts import VoiceCandidate
from .context import compile_context
from .contracts import ProposalConfigV2, ProposalConfigV3, prefix
from .subject import load


def prepare(journal, config):
    if journal.run_id != config.comparison_run_id:
        raise ContractError("local_replacement_wrong_run")
    key, raw = prefix(config.proposal_id), config.model_dump(mode="json")
    existing = journal.get(key + ":proposal")
    if existing:
        if existing["payload"]["configuration_hash"] != digest(raw):
            raise Conflict("local_replacement_proposal_changed_create_revision")
        return existing
    if not isinstance(config, ProposalConfigV2):
        raise ContractError("local_replacement_legacy_prepare_requires_contextual_revision")
    if config.parent_proposal_ref:
        previous = journal.base.read(config.parent_proposal_ref)
        old_id = previous["payload"]["configuration"]["proposal_id"]
        if journal.ref(prefix(old_id) + ":proposal") != config.parent_proposal_ref:
            raise ContractError("local_replacement_invalid_proposal_parent")
    parent, audio, params, frames, timeline, origin = load(journal, config)
    if isinstance(config, ProposalConfigV3):
        finding, units = finding_units(journal, config, timeline)
    else:
        finding, units = None, time_units(config, timeline, params)
    selectors = [item for unit in units for item in unit["script_ranges"]]
    if len(selectors) > 2:
        raise ContractError("local_replacement_selection_exceeds_two_sentences")
    start, end = units[0]["start_frame"], units[-1]["end_frame"]
    if not 0 <= start < end <= frames:
        raise ContractError("local_replacement_invalid_frame_range")
    profile = VoiceCandidate.model_validate(parent["profile"])
    source = parent["source"]["source"]
    if profile.synthesis.output_language != config.localization.recognition.language:
        raise ContractError("local_replacement_output_language_unsupported_by_localization")
    short, context_selection = compile_context(
        config.proposal_id, source, selectors, profile.synthesis.output_language
    )
    request = adapter(profile.synthesis.provider).prepare(profile.synthesis, short)
    context_start, context_end = (
        max(0, start - round(config.context_seconds * params[2])),
        min(frames, end + round(config.context_seconds * params[2])),
    )
    preview, extraction = clip(audio, context_start, context_end)
    selected, selected_extraction = clip(audio, start, end)
    value = dict(
        contract="audio-local-replacement-draft/2",
        configuration=raw,
        configuration_hash=digest(raw),
        parent_subject=config.parent_subject.model_dump(mode="json"),
        parent_audio_sha256=parent["audio_blob"]["sha256"],
        parent_audio_blob=parent["audio_blob"],
        source_ref=parent["source_ref"],
        source_script_ref=parent["source"]["source_script_ref"],
        source_script_hash=parent["source"]["source_script_hash"],
        error_source="human",
        human_reason=config.reason,
        recognition_origin=origin,
        frame_range=dict(
            start_frame=start, end_frame=end, sample_rate_hz=params[2], sample_width_bytes=params[1]
        ),
        script_ranges=selectors,
        selected_units=units,
        text="\n".join(item["text"] for item in selectors),
        profile=parent["profile"],
        synthesis_input=short.model_dump(mode="json"),
        request=request.model_dump(mode="json"),
        wire_blob=journal.blob(encode_request(request), "application/json"),
        timeline_blob=journal.blob(canonical_bytes(timeline), "application/json"),
        context_audio_blob=journal.blob(preview, "audio/wav"),
        context_extraction=extraction,
        selected_audio_blob=journal.blob(selected, "audio/wav"),
        selected_extraction=selected_extraction,
        parent_normalization=parent["playback_normalization"],
        render_policy="source_global_gain_capped_to_parent_true_peak_no_fades",
        boundary_precision="recognizer_hypothesis_or_exact_prior_replacement_group",
        tool_versions=tools(),
        production_ready=False,
    )
    if finding is not None:
        value["finding"] = finding
    value["context_selection"] = context_selection
    value["localization"] = config.localization.model_dump(mode="json")
    proposal = dict(
        contract="audio-local-replacement-proposal-record/1", proposal_hash=digest(value), payload=value
    )
    journal.put(key + ":proposal", proposal)
    return proposal


def time_units(config, timeline, params):
    point = round(config.approximate_seconds * params[2])
    index = next(
        (i for i, row in enumerate(timeline["units"]) if row["start_frame"] <= point < row["end_frame"]), None
    )
    if index is None:
        raise ContractError("local_replacement_no_sentence_at_time")
    index += config.sentence_offset
    if not 0 <= index < len(timeline["units"]) or index + config.sentence_count > len(timeline["units"]):
        raise ContractError("local_replacement_sentence_selection_outside_master")
    return timeline["units"][index : index + config.sentence_count]


def finding_units(journal, config, timeline):
    """Every spoken unit holding the finding's script words; an insertion uses its script position."""
    from ..fidelity.processing import revisions

    results = [row["result"] for row in revisions(journal, config.recognition_audit_id) if row["result"]]
    report = results[-1]["report"] if results else None
    candidate = next(
        (
            c
            for c in (report or {}).get("candidates", ())
            if c.get("candidate_id") == str(config.candidate_id)
        ),
        None,
    )
    if candidate is None or candidate["kind"] != "script_discrepancy":
        raise ContractError("local_replacement_unknown_finding")
    words = candidate["expected_tokens"]
    if not words:
        index = candidate["expected_token_range"][0]
        tokens = report["expected_tokens"]
        anchor = tokens[min(index, len(tokens) - 1)]
        at = anchor["char_start"] if index < len(tokens) else anchor["char_end"] - 1
        words = [dict(utterance_index=anchor["utterance_index"], char_start=at, char_end=at + 1)]

    def holds(unit, word):
        return any(
            item["utterance_index"] == word["utterance_index"]
            and item["char_start"] < word["char_end"]
            and word["char_start"] < item["char_end"]
            for item in unit["script_ranges"]
        )

    chosen = [i for i, unit in enumerate(timeline["units"]) if any(holds(unit, word) for word in words)]
    if not chosen or not all(any(holds(timeline["units"][i], word) for i in chosen) for word in words):
        raise ContractError("local_replacement_finding_not_in_timeline")
    if chosen != list(range(chosen[0], chosen[-1] + 1)):
        raise ContractError("local_replacement_finding_not_contiguous")
    units = timeline["units"][chosen[0] : chosen[-1] + 1]
    finding = dict(
        audit_id=str(config.recognition_audit_id),
        candidate_id=str(config.candidate_id),
        operation=candidate["operation"],
        expected_text=" ".join(t["text"] for t in candidate["expected_tokens"]),
        recognized_text=" ".join(t["text"] for t in candidate["recognized_tokens"]),
    )
    return finding, units
