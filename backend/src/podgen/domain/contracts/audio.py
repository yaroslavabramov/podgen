from typing import Literal

from pydantic import JsonValue

from .common import (
    UUID,
    ArtifactRef,
    BlobRef,
    Contract,
    DecimalString,
    DependencyDisposition,
    NonNegativeInt,
    PositiveInt,
    ScriptSelector,
    Text,
)
from .verification import FrameRange


class SpeakerBinding(Contract):
    speaker_id: UUID
    voice_profile_ref: ArtifactRef


class SpokenMapping(Contract):
    spoken_char_start: NonNegativeInt
    spoken_char_end: NonNegativeInt
    script_selector: ScriptSelector
    transformation: Literal["identity", "number_expansion", "abbreviation", "pronunciation"]
    rule_id: Text
    lexicon_entry_id: UUID | None
    equivalence_basis: Text


class Direction(Contract):
    pace_intent: Text
    emphasis: list[str]
    emotion: Text
    pause_after_ms: NonNegativeInt | None


class PerformanceUtterance(Contract):
    utterance_id: UUID
    script_selector: ScriptSelector
    spoken_text: Text
    mappings: list[SpokenMapping]
    direction: Direction


class RenderUnit(Contract):
    unit_id: UUID
    utterance_ranges: list[ScriptSelector]
    prior_context_selectors: list[ScriptSelector]
    next_context_selectors: list[ScriptSelector]
    context_mode: Text
    boundary_reason: Text
    payload_template_hash: Text


class Scene(Contract):
    scene_id: UUID
    script_selectors: list[ScriptSelector]
    dramatic_intent: Text
    listening_context: Text
    utterances: list[PerformanceUtterance]
    render_units: list[RenderUnit]


class PerformancePlan(Contract):
    episode_spec_ref: ArtifactRef
    script_ref: ArtifactRef
    verification_report_ref: ArtifactRef
    editorial_review_ref: ArtifactRef
    speaker_bindings: list[SpeakerBinding]
    lexicon_ref: ArtifactRef
    model_profile_ref: ArtifactRef
    audio_policy_ref: ArtifactRef
    delivery_profile_ref: ArtifactRef
    scenes: list[Scene]
    normalization_decisions: list[dict[str, JsonValue]]
    unresolved_issues: list[str]


class AudioSegment(Contract):
    script_ref: ArtifactRef
    performance_plan_ref: ArtifactRef
    scene_id: UUID
    unit_id: UUID
    take_id: UUID
    voice_profile_refs: list[ArtifactRef]
    model_profile_ref: ArtifactRef
    lexicon_ref: ArtifactRef
    expected_spoken_text: Text
    spoken_mappings: list[SpokenMapping]
    context_refs: list[ArtifactRef]
    render_key: Text
    provider_audio_blob: BlobRef
    pcm_blob: BlobRef
    sample_rate_hz: PositiveInt
    channels: PositiveInt
    sample_frames: PositiveInt
    provider_alignment_blob: BlobRef | None
    decoded_alignment_blob: BlobRef | None
    decode_report: dict[str, JsonValue]
    source_invocation_ids: list[UUID]
    reuse_basis_ref: ArtifactRef | None


class EditDecision(Contract):
    segment_ref: ArtifactRef
    source_start_frame: NonNegativeInt
    source_end_frame: NonNegativeInt
    timeline_start_frame: NonNegativeInt
    timeline_end_frame: NonNegativeInt
    resample_profile_ref: ArtifactRef
    gain_db: DecimalString
    transition: Text
    transition_frames: NonNegativeInt


class AudioMaster(Contract):
    script_ref: ArtifactRef
    performance_plan_ref: ArtifactRef
    episode_spec_ref: ArtifactRef
    segment_refs: list[ArtifactRef]
    assembly_profile_ref: ArtifactRef
    delivery_profile_ref: ArtifactRef
    edit_decisions: list[EditDecision]
    pcm_master_blob: BlobRef
    playback_blob: BlobRef
    additional_deliveries: list[BlobRef]
    sample_rate_hz: PositiveInt
    channels: PositiveInt
    sample_frames: PositiveInt
    measured_duration_seconds: DecimalString
    playback_decode_map: dict[str, JsonValue]
    processing_manifest_blob: BlobRef
    measurements_blob: BlobRef


class TranscriptWord(Contract):
    word_id: UUID
    spoken_text: Text
    start_frame: NonNegativeInt
    end_frame: NonNegativeInt
    speaker_id: UUID | None
    script_selectors: list[ScriptSelector]
    recognition_status: Text
    alignment_status: Text


class NonSpeechInterval(FrameRange):
    kind: Text


class Transcript(Contract):
    scope: Literal["segment", "master"]
    script_ref: ArtifactRef
    performance_plan_ref: ArtifactRef
    audio_segment_ref: ArtifactRef | None
    audio_master_ref: ArtifactRef | None
    sample_rate_hz: PositiveInt
    sample_frames: NonNegativeInt
    recognition_profile_refs: list[ArtifactRef]
    raw_recognition_blobs: list[BlobRef]
    alignment_profile_ref: ArtifactRef
    recognized_audio_sha256: Text
    decoder_profile_ref: ArtifactRef | None
    words: list[TranscriptWord]
    non_speech_intervals: list[NonSpeechInterval]
    unresolved_intervals: list[dict[str, JsonValue]]
    display_mappings: list[dict[str, JsonValue]]
    coverage_report: dict[str, JsonValue]


class MeasurementRecord(Contract):
    subject_ref: ArtifactRef
    processing_profile_ref: ArtifactRef
    measurement_blob: BlobRef


class ListeningPass(Contract):
    invocation_id: UUID
    model_profile_ref: ArtifactRef
    audio_ranges: list[FrameRange]
    assessment_blob: BlobRef


class AudioCoverage(Contract):
    expected_ranges: list[FrameRange]
    processed_ranges: list[FrameRange]
    uncovered_ranges: list[FrameRange]
    exclusions: list[dict[str, JsonValue]]


class AudioFinding(Contract):
    finding_id: UUID
    category: Text
    severity: Literal["critical", "major", "minor", "advisory"]
    blocking: bool
    policy_rule_id: Text
    sample_ranges: list[FrameRange]
    script_selectors: list[ScriptSelector]
    observation: Text
    expected: Text
    basis_refs: list[ArtifactRef]
    resolution: Literal["open", "confirmed", "dismissed"]
    rationale: Text
    recommended_action: Text


class AudioQAReport(Contract):
    scope: Literal["segment", "master"]
    script_ref: ArtifactRef
    performance_plan_ref: ArtifactRef
    transcript_ref: ArtifactRef
    audio_segment_ref: ArtifactRef | None
    audio_master_ref: ArtifactRef | None
    policy_ref: ArtifactRef
    measurement_records: list[MeasurementRecord]
    listening_passes: list[ListeningPass]
    coverage_report: AudioCoverage
    findings: list[AudioFinding]
    dependency_dispositions: list[DependencyDisposition]
    result: Literal["acceptable", "changes_required", "inconclusive"]
