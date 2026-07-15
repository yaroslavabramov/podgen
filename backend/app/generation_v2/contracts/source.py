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


class CoverageQuestionKind(StrEnum):
    CONTEXT = "context"
    ORIGIN = "origin"
    MECHANISM = "mechanism"
    CAUSE = "cause"
    CHANGE = "change"
    IMPACT = "impact"
    EXCEPTION = "exception"
    VERIFICATION = "verification"
    OTHER = "other"


class CoverageQuestionImportance(StrEnum):
    CRITICAL = "critical"
    SUPPORTING = "supporting"
    EXPLORATORY = "exploratory"


class CoverageQuestionOrigin(StrEnum):
    PLANNED = "planned"
    EMERGENT = "emergent"
    LEGACY = "legacy"


class CoverageOutcomeStatus(StrEnum):
    COVERED = "covered"
    PARTIAL = "partial"
    MISSING = "missing"
    CONFLICTED = "conflicted"
    DEFERRED = "deferred"
    UNKNOWN = "unknown"


class CoverageNextAction(StrEnum):
    RESEARCH = "research"
    SOURCE_ACQUISITION = "source_acquisition"
    OMIT = "omit"
    NONE = "none"


class SourceTopicRelevance(StrEnum):
    CORE = "core"
    SUPPORTING = "supporting"
    CONTEXTUAL = "contextual"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"


class SourceEvidenceAuthority(StrEnum):
    PRIMARY = "primary"
    OFFICIAL = "official"
    EXPERT = "expert"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"
    WEAK = "weak"
    UNKNOWN = "unknown"


class SourceContributionUniqueness(StrEnum):
    UNIQUE = "unique"
    COMPLEMENTARY = "complementary"
    DERIVATIVE = "derivative"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


class SourceProcessingStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    NEEDS_REPAIR = "needs_repair"


class SourceRepresentationStatus(StrEnum):
    UNCHECKED = "unchecked"
    READABLE = "readable"
    PARTIAL = "partial"
    UNREADABLE = "unreadable"


class SourceDisposition(StrEnum):
    ANCHOR = "anchor"
    SUPPORTING = "supporting"
    BACKGROUND = "background"
    WEAK = "weak"
    UNUSABLE = "unusable"
    DEFERRED = "deferred"


class CorpusBriefSourceSetStatus(StrEnum):
    MAPPED = "mapped"
    PARTIAL = "partial"
    DEFERRED = "deferred"
    NEEDS_REPAIR = "needs_repair"


class ResearchHandoffAction(StrEnum):
    DEEP_READ = "deep_read"
    COMPARE = "compare"
    CORROBORATE = "corroborate"
    CLOSE_GAP = "close_gap"


class CorpusBriefAuditSeverity(StrEnum):
    WARNING = "warning"
    BLOCKING = "blocking"


class CorpusBriefAuditIssueCode(StrEnum):
    UNRESOLVED_SOURCE = "unresolved_source"
    UNRESOLVED_QUESTION = "unresolved_question"
    INVALID_REFERENCE = "invalid_reference"
    UNSUPPORTED_COVERAGE = "unsupported_coverage"
    DEFERRED_SOURCE_IN_HANDOFF = "deferred_source_in_handoff"
    INCONSISTENT_SCOPE = "inconsistent_scope"
    SEMANTIC_MISMATCH = "semantic_mismatch"
    OTHER = "other"


class CorpusGapKind(StrEnum):
    TRIAGE_NOT_READY = "triage_not_ready"
    TRIAGE_ACQUISITION = "triage_acquisition"
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
    "CorpusBriefAuditIssueCode",
    "CorpusBriefAuditSeverity",
    "CorpusBriefSourceSetStatus",
    "CorpusGapKind",
    "CoverageNextAction",
    "CoverageOutcomeStatus",
    "CoverageQuestionImportance",
    "CoverageQuestionKind",
    "CoverageQuestionOrigin",
    "ResearchHandoffAction",
    "SeverityLevel",
    "SourceAcquisitionCandidateStatus",
    "SourceCardCoverageStatus",
    "SourceContributionUniqueness",
    "SourceCoverageStatus",
    "SourceDisposition",
    "SourceEvidenceAuthority",
    "SourceProcessingStatus",
    "SourceRepresentationStatus",
    "SourceRelationshipKind",
    "SourceTopicRelevance",
]
