from __future__ import annotations

from enum import StrEnum


class SourceCardCoverageStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceCoverageStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    WEAK = "weak"


class SourceRelationshipKind(StrEnum):
    PRIMARY_TO_SECONDARY = "primary_to_secondary"
    DUPLICATE = "duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    CONTRADICTS = "contradicts"
    UPDATES = "updates"
    BACKGROUND_FOR = "background_for"


class CorpusGapKind(StrEnum):
    TRIAGE_NOT_READY = "triage_not_ready"
    SOURCE_MAP_GAP = "source_map_gap"
    MISSING_FUNCTION = "missing_function"
    COVERAGE = "coverage"
    RESEARCH_AUDIT = "research_audit"
    MEMO_EVIDENCE = "memo_evidence"
    TREATMENT = "treatment"
    DURATION = "duration"


class SeverityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SourceAcquisitionCandidateStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    INGESTED = "ingested"


__all__ = [
    "CorpusGapKind",
    "SeverityLevel",
    "SourceAcquisitionCandidateStatus",
    "SourceCardCoverageStatus",
    "SourceCoverageStatus",
    "SourceRelationshipKind",
]
