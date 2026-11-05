from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

Text = Annotated[str, StringConstraints(min_length=1)]
DecimalString = Annotated[str, StringConstraints(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")]


def checked_timestamp(value: str) -> str:
    from datetime import datetime

    datetime.fromisoformat(value)
    return value


Timestamp = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"),
    AfterValidator(checked_timestamp),
]

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
ArtifactKind = Literal[
    "EpisodeSpec",
    "PolicyBundle",
    "Policy",
    "PromptDefinition",
    "ToolsetDefinition",
    "ModelProfile",
    "CapabilityReport",
    "ProcessingProfile",
    "SourceRevision",
    "SourceRepresentation",
    "EvidenceSpan",
    "SourceRelation",
    "SourceFamilyAssessment",
    "DerivedPreview",
    "ResearchNote",
    "QuestionRecord",
    "UncertaintyRecord",
    "DossierSnapshot",
    "EditorialState",
    "EditorialDecision",
    "ResearchRequest",
    "StoryPlan",
    "ScriptRevision",
    "ScriptChangeSet",
    "ListenerState",
    "ClaimSet",
    "VerificationReport",
    "EditorialReview",
    "CalculationRecord",
    "ShowNotes",
    "ShowNotesReview",
    "VoiceProfile",
    "PronunciationLexicon",
    "PerformancePlan",
    "AudioSegment",
    "AudioMaster",
    "Transcript",
    "AudioQAReport",
    "ReleaseManifest",
    "EvaluationCase",
    "EvaluationResult",
    "EvaluationAnnotation",
    "HumanAssessment",
    "DiagnosticArtifact",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True)


class ArtifactRef(Contract):
    artifact_id: UUID
    revision_id: UUID
    kind: ArtifactKind


class RuntimeRef(Contract):
    record_kind: Literal[
        "search_record",
        "tool_execution",
        "context_assembly",
        "user_input",
        "acquisition_record",
        "evaluation_reading",
    ]
    record_id: UUID


class BlobRef(Contract):
    storage_key: Text
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    byte_length: NonNegativeInt
    media_type: Text


class ModelProducer(Contract):
    kind: Literal["model"]
    invocation_id: UUID
    profile_ref: ArtifactRef


class CodeProducer(Contract):
    kind: Literal["code"]
    component: Text
    build_id: Text
    operation_id: UUID


class UserProducer(Contract):
    kind: Literal["user"]
    actor_id: UUID
    command_id: UUID


class HumanProducer(Contract):
    kind: Literal["human_review"]
    review_session_id: UUID
    rater_id: UUID


ProducerRef = Annotated[
    Union[ModelProducer, CodeProducer, UserProducer, HumanProducer], Field(discriminator="kind")
]


class ArtifactRevision(Contract):
    ref: ArtifactRef
    schema_version: Literal["1.0"]
    project_id: UUID
    episode_id: UUID | None
    input_refs: list[ArtifactRef]
    payload: dict[str, JsonValue]
    content_hash: str
    producer: ProducerRef
    created_at: Timestamp


class TextSelector(Contract):
    kind: Literal["text"]
    block_ids: list[UUID]
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    exact: str
    prefix: str
    suffix: str

    @model_validator(mode="after")
    def ordered(self):
        if self.char_end < self.char_start:
            raise ValueError("invalid_range")
        return self


def select_text(text: str, selector: TextSelector) -> str:
    if selector.char_end > len(text) or text[selector.char_start : selector.char_end] != selector.exact:
        raise ValueError("selector_mismatch")
    if not text[: selector.char_start].endswith(selector.prefix) or not text[selector.char_end :].startswith(
        selector.suffix
    ):
        raise ValueError("selector_context_mismatch")
    return selector.exact


class WholeArtifactSelector(Contract):
    artifact_ref: ArtifactRef
    whole: Literal[True]


class LocalScriptSelector(Contract):
    act_id: UUID
    block_id: UUID
    item_id: UUID
    char_start: NonNegativeInt | None
    char_end: NonNegativeInt | None

    @model_validator(mode="after")
    def offsets(self):
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("offsets_must_both_be_null_or_integers")
        if self.char_start is not None and self.char_end < self.char_start:
            raise ValueError("invalid_range")
        return self


class ScriptSelector(Contract):
    script_ref: ArtifactRef
    selector: LocalScriptSelector


class NoteTextRange(Contract):
    field: Literal["answer_markdown"]
    char_start: NonNegativeInt
    char_end: NonNegativeInt

    @model_validator(mode="after")
    def range(self):
        if self.char_end < self.char_start:
            raise ValueError("invalid_note_range")
        return self


class NoteSelector(NoteTextRange):
    note_ref: ArtifactRef


class ShowNotesSelector(Contract):
    show_notes_ref: ArtifactRef
    field: Literal[
        "title",
        "description",
        "chapter_title",
        "chapter_summary",
        "citation_metadata",
        "disclosure",
        "uncertainty_note",
    ]
    item_id: UUID | None
    char_start: NonNegativeInt
    char_end: NonNegativeInt


class AudioSelector(Contract):
    audio_ref: ArtifactRef
    start_frame: NonNegativeInt
    end_frame: NonNegativeInt

    @model_validator(mode="after")
    def audio_range(self):
        if self.audio_ref.kind not in ("AudioSegment", "AudioMaster") or self.end_frame <= self.start_frame:
            raise ValueError("invalid_audio_selector")
        return self


SubjectSelector = WholeArtifactSelector | ScriptSelector | NoteSelector | ShowNotesSelector | AudioSelector


class DependencyDisposition(Contract):
    prior_input_ref: ArtifactRef
    current_input_refs: list[ArtifactRef]
    disposition: Literal["retained", "replaced", "removed", "lineage_only"]
    subject_selectors: list[SubjectSelector]
    basis: Text

    @model_validator(mode="after")
    def replacements(self):
        if self.disposition == "replaced" and not self.current_input_refs:
            raise ValueError("replacement_required")
        if self.disposition in ("removed", "lineage_only") and self.current_input_refs:
            raise ValueError("removed_dependency_has_current_inputs")
        return self


class Finding(Contract):
    finding_id: UUID
    category: Text
    location_selectors: list[SubjectSelector]
    explanation: Text
    basis_refs: list[ArtifactRef]
    severity: Literal["critical", "major", "minor", "advisory"]
    blocking: bool
    policy_rule_id: Text
    required_outcome: Text


class Failure(Contract):
    code: Text
    message: Text
    retryable: bool
    details_ref: ArtifactRef | None


def validate_json(value):
    """Reject unsafe JSON before JCS, including lone surrogates and unsafe integers."""
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError("unsafe_integer_use_decimal_string")
    elif isinstance(value, float):
        import math

        if not math.isfinite(value):
            raise ValueError("nonfinite_number")
        if value.is_integer() and abs(value) > 2**53 - 1:
            raise ValueError("unsafe_integer_use_decimal_string")
    elif isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("json_key_must_be_string")
            validate_json(key)
            validate_json(child)
    elif isinstance(value, list):
        for child in value:
            validate_json(child)
    else:
        raise ValueError(f"not_json: {type(value).__name__}")
