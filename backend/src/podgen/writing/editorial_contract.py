"""Versioned editorial data and backend-only source bindings, never a model request by itself."""

from typing import Literal

from pydantic import Field, model_serializer, model_validator

from .planning_roles import PlanningOutput
from .roles import FullText, RoleOutput


class EditorialDuration(RoleOutput):
    target_seconds: int = Field(gt=0)
    min_seconds: int = Field(gt=0)
    max_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def order(self):
        if not self.min_seconds <= self.target_seconds <= self.max_seconds:
            raise ValueError("editorial_invalid_duration_range")
        return self


class EditorialSpeaker(RoleOutput):
    label: FullText
    function: FullText
    speech_register: FullText


class Assignment(RoleOutput):
    key: FullText
    title: FullText
    question: FullText
    audience: FullText
    listener_outcome: FullText
    format: Literal["solo", "dialogue"]
    output_language: FullText
    speakers: list[EditorialSpeaker] = Field(min_length=1)
    duration: EditorialDuration
    knowledge_cutoff: FullText
    required_topics: list[FullText]
    excluded_topics: list[FullText] = Field(default_factory=list)
    style_guidance: list[FullText] = Field(default_factory=list)
    listener_profile: FullText | None = None
    genre: FullText | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_shape(self, handler):
        data = handler(self)
        for field in ("listener_profile", "genre"):
            if data[field] is None:
                data.pop(field)
        return data


class SourceDocument(RoleOutput):
    key: FullText
    title: FullText
    source_path: FullText
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    purpose: Literal["brief", "dossier", "primary"]


class ExactExcerpt(RoleOutput):
    text: FullText
    document: FullText
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class KnowledgeCard(RoleOutput):
    label: FullText
    title: FullText
    claim: FullText
    context: FullText
    limitations: list[FullText]
    excerpts: list[ExactExcerpt] = Field(min_length=1)


class EditorialThread(RoleOutput):
    question: FullText
    explanation: FullText
    support_labels: list[FullText] = Field(min_length=1)
    limitations: list[FullText]


class EditorialDossier(RoleOutput):
    summary: FullText
    summary_support_labels: list[FullText] = Field(min_length=1)
    throughline_constraints: list[FullText]
    threads: list[EditorialThread]
    knowledge_cards: list[KnowledgeCard] = Field(min_length=1)


class EditorialInput(RoleOutput):
    contract: Literal["editorial-input/1"]
    assignment: Assignment
    documents: list[SourceDocument] = Field(min_length=1)
    dossier: EditorialDossier

    @model_validator(mode="after")
    def bindings(self):
        labels = [card.label for card in self.dossier.knowledge_cards]
        keys = [doc.key for doc in self.documents]
        if len(set(labels)) != len(labels) or len(set(keys)) != len(keys):
            raise ValueError("editorial_duplicate_label_or_document")
        if len({speaker.label for speaker in self.assignment.speakers}) != len(self.assignment.speakers):
            raise ValueError("editorial_duplicate_speaker")
        for refs in [
            self.dossier.summary_support_labels,
            *(thread.support_labels for thread in self.dossier.threads),
        ]:
            if set(refs) - set(labels):
                raise ValueError("editorial_unknown_support_label")
        for card in self.dossier.knowledge_cards:
            for excerpt in card.excerpts:
                if excerpt.document not in keys or excerpt.end <= excerpt.start:
                    raise ValueError("editorial_invalid_excerpt_binding")
                if next(doc for doc in self.documents if doc.key == excerpt.document).purpose != "primary":
                    raise ValueError("editorial_excerpt_requires_primary_document")
        return self


class FixedSourceDocument(RoleOutput):
    source_path: FullText
    sha256: str = Field(pattern="^[0-9a-f]{64}$")


class FixedEditorialInput(RoleOutput):
    contract: Literal["fixed-editorial-input/1"]
    assignment_key: FullText
    shared_input_digest: str = Field(pattern="^[0-9a-f]{64}$")
    document: FixedSourceDocument
    plan: PlanningOutput
