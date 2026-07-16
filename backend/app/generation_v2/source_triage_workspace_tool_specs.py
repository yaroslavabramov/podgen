from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_EDITORIAL_FUNCTION_LIMIT,
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_CAPSULE_PAGE_MAX_LIMIT,
    TRIAGE_PENDING_QUESTION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)


ToolHandler = Callable[[dict[str, Any]], Any]


def source_triage_planner_tools(
    list_source_capsules_handler: ToolHandler,
    route_sources_for_initial_reading_handler: ToolHandler,
    upsert_coverage_question_handler: ToolHandler,
    upsert_triage_source_set_handler: ToolHandler,
    finalize_triage_workspace_plan_handler: ToolHandler,
    *,
    terminal_resume_loader=None,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="list_source_capsules",
            description=(
                "Inspect one ambiguous source_ref or page compact orientation capsules. "
                "Text availability is mechanical and is not a source-quality decision."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": TRIAGE_CAPSULE_PAGE_MAX_LIMIT},
                    "cursor": {"type": "string"},
                    "source_refs": {
                        "type": "array",
                        "maxItems": 1,
                        "items": {"type": "string"},
                    },
                },
                "required": ["limit", "cursor", "source_refs"],
                "additionalProperties": False,
            },
            handler=list_source_capsules_handler,
        ),
        AgentTool(
            name="route_sources_for_initial_reading",
            description=(
                "Route explicitly inspected sources that still cannot be responsibly attached to a substantive "
                "coverage question. This is a neutral reading queue, not a topic, quality, or deferral judgment."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_refs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
                "required": ["source_refs"],
                "additionalProperties": False,
            },
            handler=route_sources_for_initial_reading_handler,
        ),
        AgentTool(
            name="upsert_coverage_question",
            description=(
                "Persist one question the corpus triage must orient for downstream research. "
                "Questions describe coverage to investigate; they are not prewritten conclusions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question_id": {"type": "string"},
                    "question": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["question"]},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "context",
                            "origin",
                            "mechanism",
                            "cause",
                            "change",
                            "impact",
                            "exception",
                            "verification",
                            "other",
                        ],
                    },
                    "importance": {"type": "string", "enum": ["critical", "supporting", "exploratory"]},
                    "rationale": {
                        "type": "string",
                        "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["question_rationale"],
                    },
                    "parent_question_id": {
                        "type": ["string", "null"],
                        "description": "Existing parent question_id, or null when this is a root question.",
                    },
                },
                "required": ["question", "kind", "importance", "rationale"],
                "additionalProperties": False,
            },
            handler=upsert_coverage_question_handler,
        ),
        AgentTool(
            name="upsert_triage_source_set",
            description="Persist one planned source set for later detailed triage. Use this to save grouping decisions as you page through capsules.",
            parameters={
                "type": "object",
                "properties": {
                    "set_id": {"type": "string"},
                    "label": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["label"]},
                    "purpose": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["purpose"]},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["rationale"]},
                    "question_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "priority": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["priority"]},
                },
                "required": ["label", "purpose", "source_refs", "rationale", "question_ids", "priority"],
                "additionalProperties": False,
            },
            handler=upsert_triage_source_set_handler,
        ),
        AgentTool(
            name="finalize_triage_workspace_plan",
            description=(
                "Mark the source-set plan complete. Every source_ref must belong to exactly one set, "
                "every substantive coverage question must be assigned to a coverage set, and the central root "
                "question must be declared. Source deferral is decided only after reading."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["plan_summary"]},
                    "central_question_id": {"type": "string"},
                },
                "required": ["summary", "central_question_id"],
                "additionalProperties": False,
            },
            handler=finalize_triage_workspace_plan_handler,
            terminal=True,
            terminal_result_builder=lambda arguments, _output: SourceTriageCompletion(
                ready=True,
                summary=str(arguments.get("summary") or "Source triage plan persisted."),
                notes=[],
            ),
            terminal_resume_loader=terminal_resume_loader,
        ),
    ]


def source_triage_set_tools(
    record_triage_observation_handler: ToolHandler,
    upsert_source_assessment_handler: ToolHandler,
    upsert_coverage_outcome_handler: ToolHandler,
    finish_triage_source_set_handler: ToolHandler,
    *,
    terminal_resume_loader=None,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="record_triage_observation",
            description=(
                "Persist a compact intermediate finding before reading more or crossing a checkpoint boundary. "
                "In a neutral orientation set, keep observations source-specific: queue membership is not evidence "
                "for a shared finding."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "set_id": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["note"]},
                    "pending_questions": {
                        "type": "array",
                        "maxItems": TRIAGE_PENDING_QUESTION_LIMIT,
                        "items": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["pending_question"]},
                    },
                },
                "required": ["set_id", "source_refs", "evidence_ids", "note", "pending_questions"],
            },
            handler=record_triage_observation_handler,
        ),
        source_assessment_tool(upsert_source_assessment_handler),
        coverage_outcome_tool(upsert_coverage_outcome_handler),
        finish_triage_source_set_tool(
            finish_triage_source_set_handler,
            description=(
                "Finish the current source set: mapped for completed usable work, deferred only for completed "
                "representation-blocked evaluation, or needs_repair for unfinished work."
            ),
            terminal=True,
            terminal_resume_loader=terminal_resume_loader,
        ),
    ]


def source_triage_finalizer_tools(
    finish_triage_source_set_handler: ToolHandler,
    upsert_source_assessment_handler: ToolHandler,
    upsert_coverage_outcome_handler: ToolHandler,
    upsert_coverage_synthesis_handler: ToolHandler,
) -> list[AgentTool]:
    return [
        source_assessment_tool(upsert_source_assessment_handler),
        coverage_outcome_tool(upsert_coverage_outcome_handler),
        coverage_synthesis_tool(upsert_coverage_synthesis_handler),
        finish_triage_source_set_tool(
            finish_triage_source_set_handler,
            description=(
                "Use needs_repair for unfinished repair work; deferred is only for a completed evaluation blocked "
                "by checked partial/not_target_content/unreadable representations."
            ),
            terminal=False,
        ),
    ]


def source_assessment_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="upsert_source_assessment",
        description=(
            "Persist the run-specific assessment of one source. Keep processing/readability, topic relevance, "
            "authority, uniqueness, and editorial disposition as separate judgments."
        ),
        parameters={
            "type": "object",
            "properties": {
                "set_id": {"type": "string"},
                "source_ref": {"type": "string"},
                "processing_status": {
                    "type": "string",
                    "enum": ["resolved", "needs_repair"],
                    "description": (
                        "Use resolved when this assessment is complete, including not_target_content or unreadable "
                        "with disposition=deferred. Use needs_repair only when triage work is unfinished and must be "
                        "redone."
                    ),
                },
                "representation_status": {
                    "type": "string",
                    "enum": ["unchecked", "readable", "partial", "not_target_content", "unreadable"],
                    "description": (
                        "readable/partial mean the requested source content itself is present. "
                        "not_target_content means readable text was obtained, but it is only a service, consent, "
                        "access, error, or wrong-target page rather than the requested content. unreadable means no "
                        "inspectable text was obtained. not_target_content requires unknown source-judgment axes, "
                        "empty judgment reasons/links, and disposition=deferred."
                    ),
                },
                "representation_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                },
                "relevance": {
                    "type": "string",
                    "enum": ["core", "supporting", "contextual", "out_of_scope", "unknown"],
                },
                "relevance_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                },
                "authority": {
                    "type": "string",
                    "enum": ["primary", "official", "expert", "secondary", "tertiary", "weak", "unknown"],
                    "description": (
                        "primary=original/direct record, including an official original; official=institutional "
                        "account that is not the underlying original; then expert/secondary/tertiary/weak/unknown."
                    ),
                },
                "authority_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                },
                "uniqueness": {
                    "type": "string",
                    "enum": ["unique", "complementary", "derivative", "duplicate", "unknown"],
                },
                "uniqueness_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                },
                "disposition": {
                    "type": "string",
                    "enum": ["anchor", "supporting", "background", "weak", "unusable", "deferred"],
                    "description": (
                        "deferred=value remains unknown because the checked representation is partial, "
                        "not_target_content, or unreadable; "
                        "unusable=completed reading showed usable text cannot support downstream work; "
                        "weak=usable only with an explicit caveat. Unfinished work is processing_status=needs_repair."
                    ),
                },
                "disposition_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                    "description": "Separate reason for the downstream disposition; required for weak/unusable/deferred.",
                },
                "coverage_question_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "May include any persisted run question. A newly discovered question is attached to this set "
                        "and then requires a coverage outcome before the set can finish. Must be empty for "
                        "not_target_content or unreadable because target-source relevance is unknown."
                    ),
                },
                "editorial_functions": {
                    "type": "array",
                    "maxItems": TRIAGE_EDITORIAL_FUNCTION_LIMIT,
                    "items": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["question"]},
                },
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "limitations": {
                    "type": "array",
                    "maxItems": TRIAGE_LIMITATION_LIMIT,
                    "items": {
                        "type": "string",
                        "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
                    },
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Confidence that the cited persisted evidence supports this assessment.",
                },
            },
            "required": [
                "set_id",
                "source_ref",
                "processing_status",
                "representation_status",
                "representation_reason",
                "relevance",
                "relevance_reason",
                "authority",
                "authority_reason",
                "uniqueness",
                "uniqueness_reason",
                "disposition",
                "disposition_reason",
                "coverage_question_ids",
                "editorial_functions",
                "evidence_ids",
                "limitations",
                "confidence",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def coverage_outcome_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="upsert_coverage_outcome",
        description=(
            "Persist the outcome for one assigned coverage question: covered, partial, missing, conflicted, "
            "or explicitly deferred. This is local orientation from one source set, not the final research answer "
            "or a run-wide routing decision."
        ),
        parameters={
            "type": "object",
            "properties": {
                "set_id": {"type": "string"},
                "question_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["covered", "partial", "missing", "conflicted", "deferred"],
                    "description": (
                        "partial requires both established orientation and a concrete remainder; missing means checked "
                        "but unsupported; deferred means checked partial/not_target_content/unreadable representations "
                        "prevent evaluation."
                    ),
                },
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "orientation_summary": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_summary"],
                    "description": "Established orientation; empty for missing or deferred.",
                },
                "missing_detail": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "description": "Concrete uncovered remainder; required for partial or missing.",
                },
                "conflict_summary": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                },
                "deferred_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Confidence that the cited persisted evidence supports this local outcome.",
                },
            },
            "required": [
                "set_id",
                "question_id",
                "status",
                "source_refs",
                "evidence_ids",
                "orientation_summary",
                "missing_detail",
                "conflict_summary",
                "deferred_reason",
                "confidence",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def coverage_synthesis_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="upsert_coverage_synthesis",
        description=(
            "Persist the run-wide synthesis for one coverage question after reconciling all of its per-set "
            "outcomes. This is the authoritative triage handoff to research; do not concatenate mutually "
            "obsolete missing-detail statements. The backend derives source_refs and evidence_ids from the "
            "question's current per-set outcomes, so do not copy lineage identifiers into this call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["covered", "partial", "missing", "conflicted", "deferred"],
                    "description": (
                        "partial requires both established orientation and a concrete remainder; missing means checked "
                        "but unsupported; deferred means checked partial/not_target_content/unreadable representations "
                        "prevent evaluation."
                    ),
                },
                "orientation_summary": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_summary"],
                    "description": "Run-wide established orientation; empty for missing or deferred.",
                },
                "missing_detail": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                    "description": "Concrete run-wide uncovered remainder; required for partial or missing.",
                },
                "conflict_summary": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                },
                "deferred_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Confidence that the cited persisted evidence supports this run-wide synthesis.",
                },
                "next_action": {
                    "type": "string",
                    "enum": ["research", "source_acquisition", "omit", "none"],
                    "description": (
                        "research=read/compare current corpus; source_acquisition=needed material is absent from it; "
                        "omit=do not carry forward; none=no further work."
                    ),
                },
            },
            "required": [
                "question_id",
                "status",
                "orientation_summary",
                "missing_detail",
                "conflict_summary",
                "deferred_reason",
                "confidence",
                "next_action",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def finish_triage_source_set_tool(
    handler: ToolHandler,
    *,
    description: str,
    terminal: bool,
    terminal_resume_loader=None,
) -> AgentTool:
    return AgentTool(
        name="finish_triage_source_set",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "set_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["mapped", "deferred", "needs_repair"],
                    "description": (
                        "mapped/deferred require completed assessments; deferred is representation-blocked; "
                        "needs_repair is unfinished work."
                    ),
                },
                "summary": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["findings_summary"]},
                "deferred_reason": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"]},
                "deferred_sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_ref": {"type": "string"},
                            "reason": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"]},
                        },
                        "required": ["source_ref", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["set_id", "status", "summary", "deferred_reason", "deferred_sources"],
        },
        handler=handler,
        terminal=terminal,
        terminal_result_builder=(
            lambda arguments, _output: (
                SourceTriageCompletion(
                    ready=str(arguments.get("status") or "") in {"mapped", "deferred"},
                    summary=str(arguments.get("summary") or "Source triage set persisted."),
                    notes=(
                        [str(arguments.get("deferred_reason") or "")]
                        if str(arguments.get("deferred_reason") or "").strip()
                        else []
                    ),
                )
                if terminal
                else None
            )
        ),
        terminal_resume_loader=terminal_resume_loader if terminal else None,
    )
