from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from podgen.domain.contracts.common import Contract, Text

Provider = Literal["elevenlabs", "gemini", "inworld", "fish", "minimax"]
MODELS = dict(
    elevenlabs="eleven_v4",
    gemini="gemini-3.8-flash-tts",
    inworld="inworld-tts-2",
    fish="drama-3",
    minimax="speech-2.8-hd",
)


class SparseDirection(Contract):
    """No local instruction means inherit the voice's natural delivery; legacy Direction is unchanged."""

    style: str = ""


class SynthesisProfile(Contract):
    provider: Provider
    model_id: Text
    voice_id: Text
    output_language: Literal["ru"] = "ru"
    sample_rate_hz: int = Field(default=24000, gt=0)
    output_format: Literal["wav", "pcm_s16le", "flac"] = "wav"
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exact_model(self):
        if self.model_id != MODELS[self.provider]:
            raise ValueError("unsupported_synthesis_model_no_fallback")
        return self


class SynthesisUtterance(Contract):
    utterance_id: UUID
    speaker: Text
    text: Text


class SynthesisInput(Contract):
    utterances: list[SynthesisUtterance] = Field(min_length=1)
    output_language: Literal["ru"] = "ru"
    direction: SparseDirection = Field(default_factory=SparseDirection)
    speaker_voices: dict[str, str] = Field(default_factory=dict)


class PreparedRequest(Contract):
    adapter_version: Text
    provider: Provider
    model_id: Text
    method: Literal["POST"] = "POST"
    url: Text
    headers: dict[str, str]
    body: dict[str, JsonValue]
    query: dict[str, str] = Field(default_factory=dict)
    text_characters: int = Field(ge=0)
    text_utf8_bytes: int = Field(ge=0)
    output_format: Literal["wav", "pcm_s16le", "flac"]
    sample_rate_hz: int = Field(gt=0)


@dataclass(frozen=True)
class HttpReceipt:
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    complete: bool = True
    error: str | None = None


@dataclass(frozen=True)
class SynthesizedAudio:
    audio: bytes
    media_type: str
    sample_rate_hz: int
    encoding: str
    request_id: str | None
    usage: dict[str, int]
    usage_basis: str
    alignment: dict | list | None = None


class SynthesisPort(Protocol):
    def prepare(self, profile: SynthesisProfile, source: SynthesisInput) -> PreparedRequest: ...
    def decode(self, request: PreparedRequest, receipt: HttpReceipt) -> SynthesizedAudio: ...
