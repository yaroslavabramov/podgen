"""A paired recording judgment with checkable quotations for both complete scripts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Form):
    quote: str = Field(
        min_length=1,
        description="A short exact substring from one spoken utterance, with original spelling and punctuation. No ellipses unless they occur in the source.",
    )
    observation: str = Field(
        min_length=1,
        description="What this wording or placement lets the listener experience, or what it costs them.",
    )


class RecordingRead(Form):
    decisive_passage: Evidence
    ending: Evidence
    recording_status: Literal["ready", "needs_edit", "needs_rebuilding"]


class Comparison(Form):
    first: RecordingRead
    second: RecordingRead
    judgment: Literal["first", "second", "tie", "reject_both"]
    editorial_reason: str = Field(
        min_length=1,
        description="How the consequential differences between the two full episodes determine your decision. Mention a tradeoff if one matters.",
    )
