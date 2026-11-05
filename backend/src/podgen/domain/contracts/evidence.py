from typing import Literal

from pydantic import JsonValue, model_validator

from .common import (
    UUID,
    ArtifactRef,
    BlobRef,
    Contract,
    NonNegativeInt,
    PositiveInt,
    ProducerRef,
    RuntimeRef,
    Text,
    TextSelector,
    Timestamp,
)


class ResponseMetadata(Contract):
    status: int
    content_type: Text
    etag: str | None = None
    last_modified: str | None = None


class SourceDate(Contract):
    value: Text
    precision: Literal["day", "month", "year", "timestamp", "unknown"]
    basis: Text


class Applicability(Contract):
    valid_from: str | None = None
    valid_until: str | None = None
    geographic_scope: str | None = None


class SourceRights(Contract):
    access_class: Text
    storage_policy_ref: ArtifactRef
    publication_policy_ref: ArtifactRef


class SourceRevision(Contract):
    source_id: UUID
    original_blob: BlobRef
    requested_uri: Text
    resolved_uri: Text
    redirect_chain: list[str]
    acquired_at: Timestamp
    response_metadata: ResponseMetadata
    title: str | None = None
    creators: list[str]
    publisher: str | None = None
    language: str | None = None
    published_at: SourceDate | None = None
    modified_at: SourceDate | None = None
    applicability: Applicability | None = None
    acquisition_kind: Literal["http", "browser_snapshot", "upload", "connector"]
    acquisition_record_ref: RuntimeRef
    rights: SourceRights
    source_metadata_evidence: list[dict[str, JsonValue]]
    limitations: list[str]


class Extractor(Contract):
    profile_ref: ArtifactRef
    name: Text
    version: Text
    configuration_hash: Text
    model_profile_ref: ArtifactRef | None = None


class RepresentationBlock(Contract):
    block_id: UUID
    kind: Text
    parent_id: UUID | None = None
    text_range: tuple[NonNegativeInt, NonNegativeInt] | None = None
    original_anchor: dict[str, JsonValue]
    warnings: list[str]


class ExtractionReport(Contract):
    coverage: Literal["complete", "partial", "unknown"]
    missing_regions: list[str]
    reading_order_issues: list[str]
    warnings: list[str]


class DatasetField(Contract):
    field_id: Text
    source_name: Text
    type: Literal["string", "integer", "decimal", "boolean", "date", "datetime", "json"]
    nullable: bool
    unit: str | None = None
    timezone: str | None = None


class DatasetSchema(Contract):
    fields: list[DatasetField]
    null_rules: dict[str, JsonValue]
    decimal_locale: Text
    record_identity_rule: Text


class SourceRepresentation(Contract):
    source_revision_ref: ArtifactRef
    representation_kind: Literal[
        "html_text", "document_layout", "table", "dataset", "transcript", "translation"
    ]
    content_blob: BlobRef
    canonical_text_blob: BlobRef | None = None
    extractor: Extractor
    normalization_version: Text
    blocks: list[RepresentationBlock]
    extraction_report: ExtractionReport
    derived_from_representation_ref: ArtifactRef | None = None
    language: Text
    is_transformation: bool
    dataset_schema: DatasetSchema | None = None

    @model_validator(mode="after")
    def dataset(self):
        if self.representation_kind == "dataset" and self.dataset_schema is None:
            raise ValueError("dataset_schema_required")
        return self


class TableCell(Contract):
    row_id: Text
    column_id: Text


class TableSelector(Contract):
    kind: Literal["table"]
    table_block_id: UUID
    cells: list[TableCell]
    header_block_ids: list[UUID]
    footnote_block_ids: list[UUID]


class DatasetSelector(Contract):
    kind: Literal["dataset"]
    record_ids: list[str]
    field_ids: list[str]
    original_record_locators: list[str]


class PageRegionSelector(Contract):
    kind: Literal["page_region"]
    page_index: NonNegativeInt
    bbox: tuple[float, float, float, float]
    coordinate_space: Literal["normalized_top_left"]
    render_blob_ref: BlobRef
    render_profile_ref: ArtifactRef

    @model_validator(mode="after")
    def bounds(self):
        left, top, right, bottom = self.bbox
        if not 0 <= left < right <= 1 or not 0 <= top < bottom <= 1:
            raise ValueError("invalid_page_bounds")
        return self


class TimeBase(Contract):
    numerator: PositiveInt
    denominator: PositiveInt


class TimeSelector(Contract):
    kind: Literal["time"]
    track_id: Text
    time_base: TimeBase
    origin: Literal["decoded_track_start"]
    start_tick: NonNegativeInt
    end_tick: NonNegativeInt
    stream_offset_ticks: int
    alignment_kind: Literal["decoder", "asr_estimate", "reviewed"]
    transcript_block_ids: list[UUID]

    @model_validator(mode="after")
    def ordered(self):
        if self.end_tick < self.start_tick:
            raise ValueError("invalid_time_range")
        return self


EvidenceSelector = TextSelector | TableSelector | DatasetSelector | PageRegionSelector | TimeSelector


class MediaAnchor(Contract):
    blob_ref: BlobRef
    selector: EvidenceSelector


class EvidenceSpan(Contract):
    source_revision_ref: ArtifactRef
    representation_ref: ArtifactRef
    selector: EvidenceSelector
    exact_text: str | None = None
    exact_text_hash: str | None = None
    media_anchor: MediaAnchor | None = None
    context_selector: EvidenceSelector
    extraction_warnings: list[str]
    transformation: Literal["none", "ocr", "asr", "translation"]

    @model_validator(mode="after")
    def anchor(self):
        if self.selector.kind != "text" and self.exact_text is None and self.media_anchor is None:
            raise ValueError("nontext_evidence_requires_media_anchor")
        return self


class SourceRelation(Contract):
    left_source_ref: ArtifactRef
    right_source_ref: ArtifactRef
    relation: Literal[
        "identical_content", "mirrors", "translates", "republishes", "cites", "same_underlying_dataset"
    ]
    basis_refs: list[ArtifactRef]
    method: Literal["exact_hash", "metadata", "semantic_review"]
    rationale: str | None = None
    reviewer_ref: ProducerRef | None = None


class SourceFamilyAssessment(Contract):
    member_refs: list[ArtifactRef]
    common_origin_refs: list[ArtifactRef]
    relationship_refs: list[ArtifactRef]
    scope_description: Text
    unknown_relationships: list[str]
