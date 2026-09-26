from __future__ import annotations

from enum import StrEnum


class EditorialAtomKind(StrEnum):
    TENSION = "tension"
    MECHANISM = "mechanism"
    CONTEXT = "context"
    CHANGE = "change"
    IMPACT = "impact"
    DRIVER = "driver"
    EXCEPTION = "exception"
    OPEN_QUESTION = "open_question"


class StoryWeight(StrEnum):
    CORE = "core"
    SUPPORTING = "supporting"
    CAVEAT = "caveat"


class ResearchGapCategory(StrEnum):
    MISSING_SOURCE = "missing_source"
    OPEN_REQUIREMENT = "open_requirement"
    WEAK_CORROBORATION = "weak_corroboration"
    CONTRADICTION = "contradiction"
    LEGAL_DETAIL = "legal_detail"
    CHRONOLOGY_DETAIL = "chronology_detail"
    OPTIONAL_CONTEXT = "optional_context"


class ResearchGapSeverity(StrEnum):
    BLOCKING = "blocking"
    NON_BLOCKING = "non_blocking"
    OPTIONAL = "optional"


class ResearchGapStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DEFERRED = "deferred"


class ResearchGapTreatmentRelevance(StrEnum):
    CENTRAL = "central"
    SUPPORTING = "supporting"
    MINOR = "minor"


class ResearchAuditIssueType(StrEnum):
    INSUFFICIENT_MATERIAL = "insufficient_material"
    WEAK_GROUNDING = "weak_grounding"
    MISSING_STORY_FUNCTION = "missing_story_function"
    DUPLICATE_OR_STALE_ATOM = "duplicate_or_stale_atom"
    BLOCKING_GAP = "blocking_gap"
    NEEDS_RESEARCH_REPAIR = "needs_research_repair"
    SOURCE_ACQUISITION_NEEDED = "source_acquisition_needed"
    TREATMENT_CAUTION = "treatment_caution"
    NO_ISSUE = "no_issue"
    SOURCE_GAP = "source_gap"
    LANGUAGE_OR_FORMAT = "language_or_format"
    TOOL_CONTRACT = "tool_contract"
    PLANNING_GAP = "planning_gap"
    OTHER = "other"


class ResearchAuditSeverity(StrEnum):
    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"


class ResearchAuditTargetStep(StrEnum):
    EDITORIAL_RESEARCH = "editorial_research"
    RESEARCH_FINALIZER = "research_finalizer"
    RESEARCH_AUDIT = "research_audit"
    SOURCE_ACQUISITION = "source_acquisition"
    TREATMENT = "treatment"
    NONE = "none"


__all__ = [
    "EditorialAtomKind",
    "ResearchAuditIssueType",
    "ResearchAuditSeverity",
    "ResearchAuditTargetStep",
    "ResearchGapCategory",
    "ResearchGapSeverity",
    "ResearchGapStatus",
    "ResearchGapTreatmentRelevance",
    "StoryWeight",
]
