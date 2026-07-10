from __future__ import annotations


TRIAGE_READ_TOOLS = {
    "search_corpus",
    "read_evidence",
    "read_source_window",
    "skim_source",
    "inspect_source",
    "compare_source_evidence",
}
TRIAGE_SEMANTIC_COMMIT_TOOLS = {
    "record_triage_observation",
    "finish_triage_source_set",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_source_cluster",
    "record_gap",
}
TRIAGE_READS_PER_COMMIT = 10
TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT = 8
TRIAGE_CAPSULE_PAGE_MAX_LIMIT = 12
TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS = 9000
TRIAGE_CAPSULE_PREVIEW_CHARS = 300
TRIAGE_SEMANTIC_FIELD_LIMITS = {
    "label": 180,
    "purpose": 600,
    "rationale": 1200,
    "question": 280,
    "priority": 50,
    "plan_summary": 1600,
    "note": 1600,
    "pending_question": 300,
    "findings_summary": 1600,
    "deferred_reason": 1000,
}
TRIAGE_QUESTION_LIMIT = 8
TRIAGE_PENDING_QUESTION_LIMIT = 8
