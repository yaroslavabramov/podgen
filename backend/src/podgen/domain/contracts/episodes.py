from datetime import datetime, timezone
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .common import UUID, ArtifactRef, Contract, NonNegativeInt, PositiveInt, Text, Timestamp


class Audience(Contract):
    description: Text
    assumed_knowledge: list[str]


class Speaker(Contract):
    speaker_id: UUID
    display_name: Text
    role_description: Text
    voice_profile_ref: ArtifactRef


class Duration(Contract):
    target_seconds: PositiveInt
    tolerance_seconds: NonNegativeInt
    mode: Literal["target", "strict"]

    @model_validator(mode="after")
    def tolerance(self):
        if self.tolerance_seconds >= self.target_seconds:
            raise ValueError("tolerance_must_be_less_than_target")
        return self


class SubjectPeriod(Contract):
    through: str | None

    # `from` is a reserved Python word; wire format remains `from`.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    from_: str | None = Field(alias="from")

    @model_validator(mode="after")
    def dates(self):
        from datetime import date

        for value in (self.from_, self.through):
            if value is not None:
                date.fromisoformat(value)
        if self.from_ and self.through and self.from_ > self.through:
            raise ValueError("invalid_subject_period")
        return self


class Temporal(Contract):
    mode: Literal["current", "historical_as_known", "retrospective"]
    requested_as_of: Timestamp | None
    knowledge_as_of: Timestamp
    subject_period: SubjectPeriod | None

    @model_validator(mode="after")
    def cutoff(self):
        cutoff = datetime.fromisoformat(self.knowledge_as_of)
        if cutoff > datetime.now(timezone.utc):
            raise ValueError("future_knowledge_cutoff")
        if self.requested_as_of is not None and self.requested_as_of != self.knowledge_as_of:
            raise ValueError("knowledge_cutoff_must_match_requested")
        if self.mode == "historical_as_known" and self.requested_as_of is None:
            raise ValueError("historical_cutoff_required")
        if self.mode == "retrospective" and self.subject_period is None:
            raise ValueError("retrospective_subject_period_required")
        return self


class UrlInput(Contract):
    kind: Literal["url"]
    url: Text


class UploadInput(Contract):
    kind: Literal["upload"]
    upload_id: UUID


class SourceInput(Contract):
    kind: Literal["source_revision"]
    source_revision_ref: ArtifactRef


class SuppliedInput(Contract):
    input_id: UUID
    origin: UrlInput | UploadInput | SourceInput


class Delivery(Contract):
    audio_formats: list[Literal["mp3", "wav"]]
    transcript_formats: list[Literal["json", "vtt"]]


class EpisodeSpec(Contract):
    topic: Text
    main_question: Text | None
    audience: Audience
    language: Text
    format: Literal["solo", "dialogue"]
    speakers: list[Speaker]
    duration: Duration
    temporal: Temporal
    source_mode: Literal["open_web", "supplied_only"]
    supplied_inputs: list[SuppliedInput]
    preferences: list[str]
    required_topics: list[str]
    excluded_topics: list[str]
    policy_ref: ArtifactRef
    source_policy_ref: ArtifactRef
    delivery: Delivery

    @model_validator(mode="after")
    def speaker_count(self):
        if (
            self.format == "solo"
            and len(self.speakers) != 1
            or self.format == "dialogue"
            and len(self.speakers) < 2
        ):
            raise ValueError("speaker_count_does_not_match_format")
        if len({x.speaker_id for x in self.speakers}) != len(self.speakers):
            raise ValueError("duplicate_speaker_id")
        return self


class ReleaseManifest(Contract):
    purpose: Literal["production", "evaluation"]
    evaluation_case_ref: ArtifactRef | None
    episode_spec_ref: ArtifactRef
    dossier_ref: ArtifactRef
    story_plan_ref: ArtifactRef
    script_ref: ArtifactRef
    verification_report_ref: ArtifactRef
    editorial_review_ref: ArtifactRef
    performance_plan_ref: ArtifactRef
    audio_master_ref: ArtifactRef
    audio_qa_ref: ArtifactRef
    transcript_ref: ArtifactRef
    show_notes_ref: ArtifactRef
    show_notes_review_ref: ArtifactRef
    policy_ref: ArtifactRef

    @model_validator(mode="after")
    def purpose_scope(self):
        if (self.purpose == "evaluation") != (self.evaluation_case_ref is not None):
            raise ValueError("evaluation_case_scope_mismatch")
        return self
