"""Standalone excerpt copyedit contract; not wired into production."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Anchor(ClosedModel):
    utterance_index: int = Field(ge=0)
    quote: str = Field(min_length=1)


class SpeechFinding(ClosedModel):
    kind: Literal["grammar", "wording"]
    required: bool
    anchors: list[Anchor] = Field(min_length=1)
    observation: str = Field(min_length=1)
    requested_change: str = Field(min_length=1)
    preserve: list[str] = Field(min_length=1)


class SpeechProofread(ClosedModel):
    findings: list[SpeechFinding]

    def validate_against(self, utterances):
        rows = {row["index"]: row["text"] for row in utterances}
        for finding in self.findings:
            for anchor in finding.anchors:
                if not anchor.quote.strip() or anchor.quote not in rows.get(
                    anchor.utterance_index, ""
                ):
                    raise ValueError("invalid_speech_quote_anchor")
            if not finding.observation.strip() or not finding.requested_change.strip():
                raise ValueError("empty_speech_finding")
            if not all(value.strip() for value in finding.preserve):
                raise ValueError("empty_speech_preserve")


def response_schema():
    return SpeechProofread.model_json_schema()
