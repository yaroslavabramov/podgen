"""Versioned editorial data and backend-only source bindings, never a model request by itself."""

from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from .card_catalog import catalog
from .planning_choice import PlanningChoiceOutput
from .planning_roles import PlanningOutput
from .planning_v9 import PlanningOutputV9
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
    card_id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")
    label: FullText
    title: FullText
    claim: FullText
    context: FullText
    story_facts: FullText | None = None
    narrative_excerpt_indices: list[Annotated[int, Field(ge=0, strict=True)]] | None = None
    limitations: list[FullText]
    excerpts: list[ExactExcerpt] = Field(min_length=1)

    @model_serializer(mode="wrap")
    def preserve_historical_shape(self, handler):
        data = handler(self)
        for field in ("card_id", "story_facts", "narrative_excerpt_indices"):
            if data[field] is None:
                data.pop(field)
        return data


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
    contract: Literal["editorial-input/1", "editorial-input/2"]
    assignment: Assignment
    documents: list[SourceDocument] = Field(min_length=1)
    dossier: EditorialDossier

    @model_validator(mode="after")
    def bindings(self):
        modern = self.contract == "editorial-input/2"
        for card in self.dossier.knowledge_cards:
            if "narrative_excerpt_indices" in card.model_fields_set:
                if not modern:
                    raise ValueError("editorial_narrative_excerpts_forbidden_in_v1")
                indices = card.narrative_excerpt_indices
                if indices is None:
                    raise ValueError("editorial_narrative_excerpts_must_be_indices")
                if len(set(indices)) != len(indices) or any(index >= len(card.excerpts) for index in indices):
                    raise ValueError("editorial_invalid_narrative_excerpt_selection")
            if "story_facts" in card.model_fields_set:
                if not modern:
                    raise ValueError("editorial_story_facts_forbidden_in_v1")
                if card.story_facts is None:
                    raise ValueError("editorial_story_facts_must_be_text")
            if modern and card.card_id is None:
                raise ValueError("editorial_card_id_required")
            if not modern and "card_id" in card.model_fields_set:
                raise ValueError("editorial_card_id_forbidden_in_v1")
        labels = catalog(self)
        keys = [doc.key for doc in self.documents]
        if len(set(labels)) != len(labels):
            raise ValueError(
                "editorial_duplicate_card_id" if modern else "editorial_duplicate_label_or_document"
            )
        if len(set(keys)) != len(keys):
            raise ValueError("editorial_duplicate_label_or_document")
        if len({speaker.label for speaker in self.assignment.speakers}) != len(self.assignment.speakers):
            raise ValueError("editorial_duplicate_speaker")
        for refs in [
            self.dossier.summary_support_labels,
            *(thread.support_labels for thread in self.dossier.threads),
        ]:
            if set(refs) - set(labels):
                raise ValueError("editorial_unknown_card_id" if modern else "editorial_unknown_support_label")
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
    contract: Literal["fixed-editorial-input/1", "fixed-editorial-input/2"]
    assignment_key: FullText
    shared_input_digest: str = Field(pattern="^[0-9a-f]{64}$")
    document: FixedSourceDocument
    plan: PlanningChoiceOutput | PlanningOutputV9 | PlanningOutput
