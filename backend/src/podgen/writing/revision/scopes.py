"""Both editors explicitly choose repair scale and address dependent passages."""

from typing import Literal

from pydantic import Field, model_validator

from ..fact_review import FactFeedback, SourcedFactFinding
from ..reader_feedback import ReaderFeedback, RevisionScope, check_scope
from ..roles import FullText, RoleOutput, UtteranceAnchor


class DependentPassage(RoleOutput):
    reason: FullText = Field(description="Why this passage must change with the addressed repair.")
    anchors: list[UtteranceAnchor] = Field(min_length=1)


class RevisionAssessment(RevisionScope):
    scale: Literal["none", "local", "sequence", "whole_episode"] = Field(
        description="Choose none, local, sequence or whole_episode. Local includes several independent pinpoint repairs anywhere in the script. Sequence means the actual order or development must change; count and distance do not determine scale."
    )
    dependent_passages: list[DependentPassage] = Field(
        description="Explicitly addressed dependent passages; no automatic neighboring-text permission. Empty when none are needed."
    )


class ReaderAssessment(ReaderFeedback):
    revision_scope: RevisionAssessment

    def anchored_groups(self):
        return {
            **super().anchored_groups(),
            "dependent_passages": self.revision_scope.dependent_passages,
        }


class FactAssessment(FactFeedback):
    revision_scope: RevisionAssessment

    @model_validator(mode="after")
    def consistent_scope(self):
        return check_scope(self)

    def anchored_groups(self):
        return {
            **super().anchored_groups(),
            "revision_scope": [self.revision_scope],
            "dependent_passages": self.revision_scope.dependent_passages,
        }


class SequenceRange(RoleOutput):
    anchors: list[UtteranceAnchor] = Field(
        min_length=2,
        max_length=2,
        description="First and last editable utterances, in that order. Quotes identify the boundaries; the complete inclusive range is authorized.",
    )
    reason: FullText


class PinpointAssessment(RevisionAssessment):
    scale: Literal["none", "local", "whole_episode"] = Field(
        description="Local includes multiple independent pinpoint repairs at any distance; whole_episode explicitly authorizes recomposing the complete telling."
    )


class SequenceAssessment(RevisionAssessment):
    scale: Literal["sequence"]
    sequence_ranges: list[SequenceRange] = Field(
        min_length=1,
        description="Explicit editable sequences; do not infer their boundaries from scattered findings. Text outside them remains protected except separately addressed local repairs.",
    )


class ReaderAssessmentV4(ReaderAssessment):
    revision_scope: PinpointAssessment | SequenceAssessment

    def anchored_groups(self):
        return {
            **super().anchored_groups(),
            "sequence_ranges": getattr(self.revision_scope, "sequence_ranges", []),
        }


class FactAssessmentV2(FactAssessment):
    revision_scope: PinpointAssessment | SequenceAssessment

    def anchored_groups(self):
        return {
            **super().anchored_groups(),
            "sequence_ranges": getattr(self.revision_scope, "sequence_ranges", []),
        }


class SourcedFactAssessment(FactAssessmentV2):
    findings: list[SourcedFactFinding]


def select_mode(readings):
    scales = [row["revision_scope"]["scale"] for row in readings.values()]
    if any(scale in ("sequence", "whole_episode") for scale in scales):
        if "whole_episode" in scales:
            return "structural"
        if all(
            "sequence_ranges" in row["revision_scope"]
            for row in readings.values()
            if row["revision_scope"]["scale"] == "sequence"
        ):
            return "sequence"
        return "structural"
    if "local" in scales:
        return "targeted"
    raise ValueError("revision_requires_explicit_scope")
