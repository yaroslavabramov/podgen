from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from podgen.domain.contracts.common import ArtifactRef, Contract, DecimalString, Text
from podgen.research.hosted.contracts import GeminiProfile
from podgen.research.hosted.corpus_acquisition import CorpusAcquisition
from podgen.runtime.evaluation.audio.contracts import VoiceCandidate
from podgen.runtime.evaluation.contracts import RunLimits

from .duration_reference import SavedDurationReference


class ProductionSettings(Contract):
    research_instructions: Text
    research_profile: GeminiProfile
    acquisition: CorpusAcquisition
    writer_profile_ref: ArtifactRef
    reviewer_profile_ref: ArtifactRef
    planner_profile_ref: ArtifactRef
    plan_reviewer_profile_ref: ArtifactRef
    audio_profile: VoiceCandidate
    writing_limits: RunLimits
    max_research_jobs: int = Field(ge=1, le=20)
    audio_max_cost_usd: DecimalString
    audio_max_part_characters: int = Field(default=6000, ge=256, le=20000)
    accepts_variable_research_cost: Literal[True]
    duration_reference: SavedDurationReference | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_shape(self, handler):
        data = handler(self)
        if data.get("duration_reference") is None:
            data.pop("duration_reference", None)
        return data

    @model_validator(mode="after")
    def valid_profiles_and_budget(self):
        if Decimal(self.audio_max_cost_usd) <= 0:
            raise ValueError("episode_audio_budget_must_be_positive")
        if (
            any(
                ref.kind != "ModelProfile"
                for ref in (
                    self.writer_profile_ref,
                    self.reviewer_profile_ref,
                    self.planner_profile_ref,
                    self.plan_reviewer_profile_ref,
                )
            )
            or self.acquisition.profile_ref.kind != "ProcessingProfile"
        ):
            raise ValueError("episode_profile_kind_mismatch")
        if self.duration_reference is not None:
            from podgen.audio.synthesis.contracts import SparseDirection

            self.duration_reference.require_voice(self.audio_profile.synthesis, SparseDirection())
        if self.acquisition.reuse_selection_ids:
            raise ValueError("new_episode_cannot_reuse_another_reports_selection")
        return self


class ProductionConfig(Contract):
    contract: Literal["episode-production/1"] = "episode-production/1"
    run_id: UUID
    topic: Text
    output_language: Text = "ru"
    format: Literal["solo"] = "solo"
    speaker_label: Text = "РАССКАЗЧИК"
    audience: Text = "Любознательный взрослый слушатель без специальной подготовки."
    knowledge_cutoff: Text = Field(default_factory=lambda: datetime.now(timezone.utc).date().isoformat())
    min_seconds: int = Field(default=720, gt=0)
    target_seconds: int = Field(default=810, gt=0)
    max_seconds: int = Field(default=900, gt=0)
    settings: ProductionSettings

    @model_validator(mode="after")
    def ordered_duration(self):
        if not self.min_seconds <= self.target_seconds <= self.max_seconds:
            raise ValueError("episode_duration_out_of_order")
        if self.settings.audio_profile.synthesis.output_language != self.output_language:
            raise ValueError("episode_voice_language_mismatch")
        return self
