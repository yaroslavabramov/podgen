from __future__ import annotations


# Trace classifications only. They do not impose read or write-call limits.
TRIAGE_READ_TOOLS = {
    "search_corpus",
    "read_source",
    "search_source",
    "open_source_file",
    "find_quotes",
    "read_evidence",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "skim_source",
    "inspect_source",
    "compare_source_evidence",
    "compare_evidence_for_question",
}
TRIAGE_SEMANTIC_COMMIT_TOOLS = {
    "save_reading_note",
    "save_semantic_review_self_check",
    "acknowledge_semantic_review",
    "save_source_analysis",
    "save_coverage_hint",
    "upsert_coverage_outcome",
    "resolve_triage_question",
    "update_source_finding_links",
    "update_source_finding_relations",
    "audit_source_finding",
    "upsert_coverage_synthesis",
    "finish_triage_source_set",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_source_cluster",
    "record_gap",
    "resolve_gap",
    "resolve_source_map_repair",
}

TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT = 8
TRIAGE_CAPSULE_PAGE_MAX_LIMIT = 12
TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS = 9000
TRIAGE_CAPSULE_PREVIEW_CHARS = 300
TRIAGE_SEMANTIC_FIELD_LIMITS = {
    "label": 180,
    "purpose": 600,
    "rationale": 1200,
    "question": 280,
    "question_rationale": 800,
    "priority": 50,
    "plan_summary": 1600,
    "note": 1600,
    "pending_question": 300,
    "findings_summary": 1600,
    "deferred_reason": 1000,
    "assessment_reason": 1200,
    "assessment_limitation": 800,
    "no_findings_reason": 1200,
    "finding_statement": 2400,
    "finding_scope": 800,
    "finding_link_rationale": 1000,
    "finding_relation_rationale": 1200,
    "finding_support_explanation": 1600,
    "reading_basis": 2400,
    "question_disposition_rationale": 1200,
    "requirement_description": 800,
    "requirement_rationale": 1000,
    "requirement_completion_criterion": 1200,
    "requirement_summary": 1600,
    "requirement_closure_basis": 2000,
    "requirement_uncertainty": 1600,
    "coverage_summary": 1600,
    "coverage_detail": 1200,
    "coverage_hint_note": 1200,
    "coverage_synthesis_summary": 2400,
    "coverage_synthesis_detail": 1600,
    "semantic_review_reason": 1600,
    "semantic_review_conclusion": 2400,
}
TRIAGE_PENDING_QUESTION_LIMIT = 8
TRIAGE_LIMITATION_LIMIT = 12
TRIAGE_EDITORIAL_FUNCTION_LIMIT = 12
