from __future__ import annotations

from app.generation_v2.contracts.research import (
    ResearchGapCategory,
    ResearchGapSeverity,
    ResearchGapStatus,
    ResearchGapTreatmentRelevance,
    StoryWeight,
)


RESEARCH_GAP_CATEGORIES = {item.value for item in ResearchGapCategory}
RESEARCH_GAP_SEVERITIES = {item.value for item in ResearchGapSeverity}
RESEARCH_GAP_STATUSES = {item.value for item in ResearchGapStatus}
RESEARCH_GAP_TREATMENT_RELEVANCE = {item.value for item in ResearchGapTreatmentRelevance}
RESEARCH_ATOM_STORY_WEIGHTS = {item.value for item in StoryWeight}
SEMANTIC_FIELD_LIMITS = {
    "title": 260,
    "claim": 2400,
    "explanation": 3200,
    "story_value": 1600,
    "risk": 1800,
    "treatment_role_hint": 600,
    "note": 3200,
    "question": 1000,
    "resolution_note": 1800,
    "thesis": 2400,
    "research_synthesis": 4000,
    "source_coverage_item": 1000,
    "risk_register_item": 1800,
    "label": 180,
    "story_job": 80,
    "purpose": 900,
    "plan_summary": 1800,
    "pending_question": 360,
    "quote_ref": 200,
    "anchor_ref": 120,
    "job_summary": 1600,
    "deferred_reason": 1000,
    "priority": 50,
}
RESEARCH_QUESTION_LIMIT = 10
RESEARCH_PENDING_QUESTION_LIMIT = 8
RESEARCH_REQUIRED_ATOM_KIND_LIMIT = 8
RESEARCH_QUOTE_REF_LIMIT = 8
RESEARCH_ANCHOR_REF_LIMIT = 8
RESEARCH_MEMO_SOURCE_COVERAGE_LIMIT = 40
RESEARCH_MEMO_RISK_REGISTER_LIMIT = 40
RESEARCH_READS_PER_COMMIT_WARNING = 6
RESEARCH_READS_PER_COMMIT_ADVISORY = 8
