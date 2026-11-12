from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from podgen.domain.contracts.common import Contract, Text


class Case(Contract):
    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    audio_path: Path
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_text: str = Field(min_length=1, max_length=2000)
    provenance: dict[str, JsonValue]
    evaluation: dict[str, JsonValue]

    @model_validator(mode="after")
    def reference(self):
        if not self.reference_text.strip() or len(self.reference_text.split()) > 320:
            raise ValueError("toneperfect_reference_limit")
        return self


class Trial(Contract):
    contract: Literal["toneperfect-qualification/1"] = "toneperfect-qualification/1"
    run_id: UUID
    max_credits: int = Field(default=10, ge=1, le=10)
    credential_env: Text = "TONEPERFECT_API_KEY"
    timeout_seconds: float = Field(default=100, ge=27, le=180)
    max_attempts_per_case: int = Field(default=3, ge=1, le=3)
    cases: list[Case] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def unique(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("toneperfect_duplicate_case_id")
        return self
