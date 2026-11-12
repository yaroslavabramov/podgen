import json
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from podgen.audio.synthesis.contracts import Provider, SparseDirection, SynthesisProfile
from podgen.domain.contracts.common import Contract, DecimalString, RuntimeRef, Text

VERSION = "audio-comparison/1"


class PriceRate(Contract):
    unit: Literal["characters", "utf8_bytes", "input_tokens", "audio_tokens"]
    usd_per_million: DecimalString
    source_url: Text
    checked_at: Text

    @model_validator(mode="after")
    def positive_rate(self):
        if Decimal(self.usd_per_million) < 0:
            raise ValueError("negative_audio_price")
        return self


class VoiceCandidate(Contract):
    profile_id: Text
    synthesis: SynthesisProfile
    credential_env: Text
    rates: list[PriceRate]
    reservation_usd: DecimalString

    @model_validator(mode="after")
    def unique_units(self):
        if Decimal(self.reservation_usd) <= 0 or len({r.unit for r in self.rates}) != len(self.rates):
            raise ValueError("invalid_audio_cost_reservation")
        return self


class UtteranceSelection(Contract):
    utterance_index: int = Field(ge=0)
    start_char: int = Field(default=0, ge=0)
    end_char: int | None = Field(default=None, gt=0)


class Fragment(Contract):
    fragment_id: Text
    outcome_ref: RuntimeRef
    selections: list[UtteranceSelection] = Field(min_length=1)
    direction: SparseDirection = Field(default_factory=SparseDirection)


class ComparisonConfig(Contract):
    contract: Literal["audio-comparison/1"] = VERSION
    run_id: UUID
    providers: list[Provider] = Field(min_length=1)
    profiles: list[VoiceCandidate]
    fragments: list[Fragment] = Field(min_length=2, max_length=2)
    max_cost_usd: DecimalString
    max_attempts: int = Field(default=20, ge=1)
    loudness_lufs: float = Field(default=-20, ge=-30, le=-14)
    true_peak_dbtp: float = Field(default=-1, ge=-3, le=-1)
    blind_seed: int = Field(ge=0)

    @property
    def voice_counts(self):
        return {provider: 2 for provider in self.providers}

    @property
    def expected_samples(self):
        return sum(self.voice_counts.values()) * len(self.fragments)

    @property
    def http_timeout_seconds(self):
        return 180

    @model_validator(mode="after")
    def complete_names(self):
        if len(set(self.providers)) != len(self.providers):
            raise ValueError("duplicate_audio_provider")
        if len({p.profile_id for p in self.profiles}) != len(self.profiles):
            raise ValueError("duplicate_voice_profile")
        if len({f.fragment_id for f in self.fragments}) != len(self.fragments):
            raise ValueError("duplicate_audio_fragment")
        if any(p.synthesis.provider not in self.providers for p in self.profiles):
            raise ValueError("audio_profile_outside_allowed_providers")
        for provider in self.providers:
            if sum(p.synthesis.provider == provider for p in self.profiles) > self.voice_counts.get(
                provider, 0
            ):
                raise ValueError("audio_candidates_exceed_expected_voice_count")
        if Decimal(self.max_cost_usd) <= 0:
            raise ValueError("invalid_audio_budget")
        return self


class TargetedComparisonConfig(ComparisonConfig):
    contract: Literal["audio-comparison/2"] = "audio-comparison/2"
    fragments: list[Fragment] = Field(min_length=1, max_length=2)
    expected_voices_per_provider: dict[Provider, Annotated[int, Field(ge=1, le=2)]]
    request_timeout_seconds: float = Field(default=180, ge=1, le=1800)

    @property
    def voice_counts(self):
        return self.expected_voices_per_provider

    @property
    def http_timeout_seconds(self):
        return self.request_timeout_seconds

    @model_validator(mode="after")
    def explicit_voice_counts(self):
        if set(self.expected_voices_per_provider) != set(self.providers):
            raise ValueError("audio_expected_voice_counts_must_match_providers")
        return self


def load_comparison_config(text):
    raw = json.loads(text)
    # Omitted discriminator remains the original /1 contract, including its exact serialization.
    if raw.get("contract", VERSION) == "audio-comparison/2":
        return TargetedComparisonConfig.model_validate(raw)
    return ComparisonConfig.model_validate(raw)
