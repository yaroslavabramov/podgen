"""An explicit editorial choice of repair scale, never inferred from text statistics."""

from typing import Literal

from pydantic import Field, model_validator

from .roles import EditorialFeedback, FullText, RoleOutput, UtteranceAnchor


class RevisionScope(RoleOutput):
    """Choose the scale from the complete text. Keep means no required repair; local praise does not settle the scale."""

    scale: Literal["none", "local", "sequence", "whole_episode"] = Field(
        description="Reader's chosen scale of required repair: none, a passage, a connected sequence, or the whole telling."
    )
    reason: FullText = Field(
        description="Concrete editorial reason for this scale and its intended effect on the listening experience; do not infer quality from counts."
    )
    anchors: list[UtteranceAnchor] = Field(
        description="Exact passages supporting this choice. Required repair needs at least one; none may use an empty list."
    )

    @model_validator(mode="after")
    def addressed_repair(self):
        if self.scale != "none" and not self.anchors:
            raise ValueError("revision_scope_requires_anchor")
        return self


class ReaderFeedback(EditorialFeedback):
    revision_scope: RevisionScope

    @model_validator(mode="after")
    def consistent_scope(self):
        if self.recommendation == "keep" and (
            self.revision_scope.scale != "none"
            or any(f.priority in ("blocking", "important") for f in self.findings)
        ):
            raise ValueError("keep_cannot_require_revision")
        if self.recommendation == "revise" and self.revision_scope.scale == "none":
            raise ValueError("revise_requires_revision_scope")
        return self

    def anchored_groups(self):
        return {**super().anchored_groups(), "revision_scope": [self.revision_scope]}
