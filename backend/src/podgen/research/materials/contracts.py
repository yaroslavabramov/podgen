"""Section-backed research input; no KnowledgeCard or synthetic source excerpts."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from podgen.domain.contracts.common import ArtifactRef, Contract, RuntimeRef, Text
from podgen.writing.editorial_contract import Assignment, SourceDocument

PACKET = "research-editorial-input/1"
PERSISTED = "persisted-research-editorial-input/1"
VERSION = "hosted-research-materials/1"


class ReportOccurrence(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    reported_url: Text
    source_id: Text


class ReportSection(Contract):
    material_id: Text
    document: Text
    title: Text
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    kind: Literal["research_report_section", "research_review_note"] = "research_report_section"
    parent_title: str | None = None
    occurrences: list[ReportOccurrence] = Field(default_factory=list)


class ResearchSource(Contract):
    source_id: Text
    reported_url: Text
    report_number: int | None = None
    reported_label: str | None = None
    title: Text
    title_origin: Literal["reported_label", "source_metadata", "retained_heading"] = "reported_label"
    title_range: tuple[int, int] | None = None
    status: Literal["available", "partial", "unavailable", "not_acquired"]
    acquisition_status: str | None = None
    acquisition_role: Literal["original", "mediawiki", "jina", "promoted_pdf", "browser"] | None = None
    document: Text | None = None
    source_revision_ref: ArtifactRef | None = None
    representation_ref: ArtifactRef | None = None
    acquisition_record_ref: RuntimeRef | None = None
    coverage: str | None = None
    limitations: list[str] = Field(default_factory=list)
    original_occurrences: list[dict] = Field(default_factory=list)
    resolved_url: str | None = None


class ResearchPacket(Contract):
    contract: Literal["research-editorial-input/1"] = PACKET
    assignment: Assignment
    research_assignment: dict
    documents: list[SourceDocument]
    sections: list[ReportSection]
    sources: list[ResearchSource]
    report_result_ref: RuntimeRef
    source_result_refs: list[RuntimeRef]


class SourceAssessment(Contract):
    report_number: int = Field(gt=0)
    status: Literal["partial", "unavailable"]
    observation: Text


class PrepareResearchMaterials(Contract):
    contract: Literal["research-material-preparation/1"] = "research-material-preparation/1"
    operation_id: UUID
    research_run_id: UUID
    source_selection_ids: list[UUID] = Field(default_factory=list)
    assignment: Assignment
    source_assessments: list[SourceAssessment] = Field(default_factory=list)


def is_research(packet):
    return getattr(packet, "contract", None) == PACKET
