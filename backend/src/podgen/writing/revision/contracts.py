"""Private model outputs expand to the unchanged public WriteOutput contract."""

from typing import Literal

from pydantic import Field

from ..roles import FullText, RoleOutput, WriteOutput, WriteUtterance

VERSION = "addressed-script-revision/1"
FACT_V1 = "fact-revision/1"
FACT_VERSION = "fact-revision/2"


class Composition(RoleOutput):
    """The working narrative choices selected with this script, not new client requirements."""

    main_question: FullText
    listener_promise: FullText
    route_summary: FullText
    decisions: list[FullText] = Field(
        description="Selected changes of emphasis, order or disclosure and their reasons; may be empty."
    )


class OpenResearchQuestion(RoleOutput):
    question: FullText
    why_needed: FullText


class CompositionState(Composition):
    unresolved_questions: list[OpenResearchQuestion] = Field(default_factory=list)


class ReplaceText(RoleOutput):
    operation: Literal["replace"]
    target_id: FullText
    text: FullText = Field(description="Complete replacement for this offered text range only.")


class DeleteText(RoleOutput):
    operation: Literal["delete"]
    target_id: FullText


class InsertUtterances(RoleOutput):
    operation: Literal["insert"]
    target_id: FullText
    utterances: list[WriteUtterance] = Field(min_length=1)


class TargetedRevision(RoleOutput):
    edits: list[ReplaceText | DeleteText | InsertUtterances] = Field(min_length=1)
    composition_update: Composition | None


class StructuralRevision(RoleOutput):
    script: WriteOutput
    composition: Composition


class SequenceEdit(RoleOutput):
    range_id: FullText
    utterances: list[WriteUtterance]


class SequenceRevision(RoleOutput):
    sequences: list[SequenceEdit] = Field(min_length=1)
    edits: list[ReplaceText | DeleteText | InsertUtterances]
    composition: Composition
