import math
from typing import Literal

from pydantic import Field, model_validator

from podgen.domain.contracts.common import Contract, Text

VERSION = "fish-unprompted-recognition/1"


class RecognitionProfile(Contract):
    provider: Literal["fish"] = "fish"
    model_id: Literal["transcribe-1-pro"] = "transcribe-1-pro"
    language: Literal["ru"] = "ru"
    ignore_timestamps: Literal[False] = False
    diarize: Literal[False] = False
    tag_audio_events: Literal[False] = False
    request_timeout_seconds: float = Field(default=900, ge=1, le=1800)


class RecognizedSegment(Contract):
    text: str
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end < self.start:
            raise ValueError("recognition_segment_end_before_start")
        return self


class Recognition(Contract):
    text: str
    duration: float = Field(ge=0, allow_inf_nan=False)
    segments: list[RecognizedSegment]
    request_id: Text

    @model_validator(mode="after")
    def finite_duration(self):
        if not math.isfinite(self.duration):
            raise ValueError("nonfinite_recognition_duration")
        return self
