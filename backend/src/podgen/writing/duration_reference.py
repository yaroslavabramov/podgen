"""One retained voice measurement, frozen as approximate editorial input."""

from typing import Literal

from pydantic import Field, model_validator

from podgen.audio.synthesis.contracts import SparseDirection, SynthesisProfile
from podgen.domain.contracts.common import Contract

VERSION = "selected-voice-duration/1"


class VoiceDurationEstimate(Contract):
    contract: Literal["selected-voice-duration/1"] = VERSION
    synthesis: SynthesisProfile
    direction: SparseDirection
    script_word_count: int = Field(gt=0, strict=True)
    sample_frames: int = Field(gt=0, strict=True)
    sample_rate_hz: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def word_count_language(self):
        language = self.synthesis.output_language.lower().replace("_", "-").split("-")[0]
        if language not in ("ru", "en"):
            raise ValueError("duration_estimate_requires_word_count_language")
        return self

    @property
    def seconds(self):
        return self.sample_frames / self.sample_rate_hz

    def words_for(self, seconds):
        return seconds * self.script_word_count * self.sample_rate_hz // self.sample_frames

    def seconds_for(self, words):
        denominator = self.script_word_count * self.sample_rate_hz
        return (words * self.sample_frames + denominator - 1) // denominator
