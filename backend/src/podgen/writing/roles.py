"""Model-facing contracts for baseline A: draft, independent review, full revision.

The caller pins and archives the actual instructions, full inputs and outputs.
It schedules review in a separate invocation, enforces source access and any
blind-reading phases, and binds these wire outputs to canonical artifacts.
These models do not grant factual, editorial or audio acceptance.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from .prompts import role_instructions as role_instructions
from .prompts import role_instructions_for_version as role_instructions_for_version

WritingStage = Literal["draft", "review", "revise"]
WritingFormat = Literal["solo", "dialogue"]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text_must_not_be_blank")
    return value


# Validate presence without trimming or imposing semantic size limits.
FullText = Annotated[str, StringConstraints(min_length=1), AfterValidator(_nonblank)]


class RoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class WriteUtterance(RoleOutput):
    speaker: FullText = Field(description="Exact speaker identifier from the supplied assignment.")
    text: FullText = Field(description="Complete spoken words, without speaker labels or editorial notes.")


class WriteOutput(RoleOutput):
    """One complete episode in playback order, never a summary or a patch."""

    utterances: list[WriteUtterance] = Field(min_length=1)


class UtteranceAnchor(RoleOutput):
    utterance_index: Annotated[int, Field(ge=0)] = Field(
        description="Zero-based position in the exact supplied WriteOutput.utterances list."
    )
    quote: FullText = Field(description="Exact nonempty substring of that utterance's spoken text.")


class EditorialStrength(RoleOutput):
    anchors: list[UtteranceAnchor] = Field(min_length=1)
    reason: FullText = Field(description="What works for the listener and should survive revision.")


class EditorialFinding(RoleOutput):
    dimension: Literal[
        "promise",
        "mechanism",
        "structure",
        "transition",
        "repetition",
        "oral_style",
        "speaker_contribution",
        "factuality",
        "attribution",
        "duration",
        "ending",
    ]
    priority: Literal["blocking", "important", "optional"]
    anchors: list[UtteranceAnchor] = Field(min_length=1)
    observation: FullText = Field(description="Specific defect in the words the listener receives.")
    requested_change: FullText = Field(description="Actionable repair and intended change in understanding.")
    preserve: list[FullText] = Field(description="Useful properties of the passage to retain; may be empty.")


class ResearchNeed(RoleOutput):
    anchors: list[UtteranceAnchor] = Field(min_length=1)
    question: FullText
    why_needed: FullText


class EditorialFeedback(RoleOutput):
    """Actionable text review, not a fact-check report or evidence of human listening."""

    reading_mode: Literal["script_only", "informed"] = Field(
        description="script_only only if plan, dossier, author notes and prior reviews were not visible."
    )
    listener_answer: FullText = Field(description="Main question and answer reconstructed from spoken words.")
    important_limits: list[FullText]
    strengths: list[EditorialStrength]
    findings: list[EditorialFinding]
    research_requests: list[ResearchNeed]
    duration_note: FullText = Field(
        description="Evidence and uncertainty about duration; never invented timing."
    )
    recommendation: Literal["keep", "revise", "needs_material"]

    def validate_against(self, script: WriteOutput) -> None:
        """Check wire anchors against the exact input before passing feedback on.

        This proves location/quote identity, not the reviewer's interpretation.
        Indexes cannot be reused automatically after revision or reordering.
        """
        for entry in (*self.strengths, *self.findings, *self.research_requests):
            for anchor in entry.anchors:
                if anchor.utterance_index >= len(script.utterances):
                    raise ValueError("feedback_utterance_index_out_of_range")
                if anchor.quote not in script.utterances[anchor.utterance_index].text:
                    raise ValueError("feedback_quote_mismatch")
