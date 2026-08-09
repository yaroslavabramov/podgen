from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_EDITORIAL_FUNCTION_LIMIT,
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_CAPSULE_PAGE_MAX_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_source_analysis_tool_schema import (
    reading_basis_schema,
)
from app.generation_v2.source_triage_grounding_audit_tool import (
    source_finding_grounding_audit_tool,
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
                    "answer_requirements": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "Free-form semantic parts that must be resolved for this question to have a complete "
                            "answer. Use the number the question needs; there is no quota or fixed taxonomy."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "requirement_id": {"type": "string"},
                                "description": {
                                    "type": "string",
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_description"],
                                },
                                "rationale": {
                                    "type": "string",
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_rationale"],
                                },
                                "completion_criterion": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS[
                                        "requirement_completion_criterion"
                                    ],
                                    "description": (
                                        "State what evidence or explanatory link would make this answer part "
                                        "genuinely resolved; chronology or adjacent context is not enough for a "
                                        "causal requirement."
                                    ),
                                },
                            },
                            "required": ["description", "rationale", "completion_criterion"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["question", "kind", "importance", "rationale", "answer_requirements"],
                "additionalProperties": False,
            },
            handler=upsert_coverage_question_handler,
        ),
        AgentTool(
            name="upsert_triage_source_set",
            description=(
                "Persist one reading-responsibility queue. It assigns each source to exactly one future set agent; "
                "it is not a thematic boundary on what that source may support."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "set_id": {"type": "string"},
                    "label": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["label"]},
                    "purpose": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["purpose"]},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["rationale"]},
                    "priority": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["priority"]},
                },
                "required": [
                    "label",
                    "purpose",
                    "source_refs",
                    "rationale",
                    "priority",
                ],
                "additionalProperties": False,
            },
            handler=upsert_triage_source_set_handler,
        ),
        AgentTool(
            name="finalize_triage_workspace_plan",
            description=(
                "Mark the source-set plan complete. Every source_ref must belong to exactly one set, "
                "and the central root question must be declared. Source deferral is decided only after reading."
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
    save_source_analysis_handler: ToolHandler,
    save_coverage_hint_handler: ToolHandler,
    finish_triage_source_set_handler: ToolHandler,
    *,
    save_reading_note_handler: ToolHandler | None = None,
    save_semantic_review_self_check_handler: ToolHandler | None = None,
    allowed_source_refs: list[str] | None = None,
    allowed_question_ids: list[str] | None = None,
    terminal_resume_loader=None,
) -> list[AgentTool]:
    tools = [
        source_analysis_tool(
            save_source_analysis_handler,
            allowed_source_refs=allowed_source_refs,
            allowed_question_ids=allowed_question_ids,
        ),
        coverage_hint_tool(
            save_coverage_hint_handler,
            allowed_source_refs=allowed_source_refs,
            allowed_question_ids=allowed_question_ids,
        ),
    ]
    tools.append(
        finish_triage_source_set_tool(
            finish_triage_source_set_handler,
            description=(
                "Finish the current source set after every source has a saved analysis, duplicate, or representation "
                "deferral, and all delivered source reading has been reconciled into durable memory."
            ),
            terminal=True,
            terminal_resume_loader=terminal_resume_loader,
        )
    )
    if save_reading_note_handler is not None:
        tools.insert(
            0,
            reading_note_tool(
                save_reading_note_handler,
                allowed_source_refs=allowed_source_refs,
                allowed_question_ids=allowed_question_ids,
            ),
        )
    return tools


def source_mapper_tools(
    save_source_analysis_handler: ToolHandler,
    finish_source_mapping_handler: ToolHandler,
    *,
    allowed_source_refs: list[str] | None = None,
    allowed_question_ids: list[str] | None = None,
    terminal_resume_loader=None,
) -> list[AgentTool]:
    return [
        source_mapper_analysis_tool(
            save_source_analysis_handler,
            allowed_source_refs=allowed_source_refs,
            allowed_question_ids=allowed_question_ids,
        ),
        finish_source_mapping_tool(
            finish_source_mapping_handler,
            terminal_resume_loader=terminal_resume_loader,
        ),
    ]


def source_mapper_analysis_tool(
    handler: ToolHandler,
    *,
    allowed_source_refs: list[str] | None = None,
    allowed_question_ids: list[str] | None = None,
) -> AgentTool:
    return AgentTool(
        name="save_source_analysis",
        description=(
            "Persist the current accepted SourceAnalysis for exactly one source. This is source mapping only: "
            "findings use question_ids as relevance tags, not coverage decisions. A readable but irrelevant source "
            "may be saved as usable with empty findings and a concrete status_reason."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_ref": _bounded_id_schema(allowed_source_refs),
                "status": {"type": "string", "enum": ["usable", "deferred", "not_target_content"]},
                "status_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                },
                "summary": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["findings_summary"],
                },
                "source_limitations": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
                    },
                },
                "read_state": {
                    "type": "object",
                    "properties": {
                        "complete": {"type": "boolean"},
                        "unread_regions": {"type": "array", "items": {"type": "string"}},
                        "stopping_reason": {"type": "string"},
                    },
                    "required": ["complete", "unread_regions", "stopping_reason"],
                    "additionalProperties": False,
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_id": {
                                "type": "string",
                                "description": "Optional stable ID. Leave empty unless replacing a known finding.",
                            },
                            "statement": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_statement"],
                            },
                            "evidence_ids": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {"type": "string"},
                            },
                            "passage_refs": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                                "description": (
                                    "Stable passage_ref values returned by read_source. Backend turns them into "
                                    "EvidenceItems without another model read."
                                ),
                            },
                            "question_ids": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": _bounded_id_schema(allowed_question_ids),
                            },
                            "limitations": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
                                },
                            },
                        },
                        "required": [
                            "statement",
                            "evidence_ids",
                            "passage_refs",
                            "question_ids",
                            "limitations",
                        ],
                        "additionalProperties": False,
                    },
                },
                "gaps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question_id": _bounded_id_schema(allowed_question_ids),
                            "description": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                            },
                        },
                        "required": ["question_id", "description"],
                        "additionalProperties": False,
                    },
                },
                "duplicate_of_source_ref": {
                    "type": "string",
                    "description": (
                        "Only copy a backend-provided exact duplicate source_ref, usually from read_source guidance. "
                        "Leave empty otherwise."
                    ),
                },
            },
            "required": [
                "source_ref",
                "status",
                "status_reason",
                "summary",
                "source_limitations",
                "read_state",
                "findings",
                "gaps",
                "duplicate_of_source_ref",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def finish_source_mapping_tool(
    handler: ToolHandler,
    *,
    terminal_resume_loader=None,
) -> AgentTool:
    return AgentTool(
        name="finish_source_mapping",
        description="Finish mapping one source after save_source_analysis has been accepted for that source_ref.",
        parameters={
            "type": "object",
            "properties": {
                "source_ref": {"type": "string"},
                "summary": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["findings_summary"],
                },
            },
            "required": ["source_ref", "summary"],
            "additionalProperties": False,
        },
        handler=handler,
        terminal=True,
        terminal_result_builder=lambda arguments, _output: SourceTriageCompletion(
            ready=True,
            summary=str(arguments.get("summary") or "Source mapping persisted."),
            notes=[],
        ),
        terminal_resume_loader=terminal_resume_loader,
    )


def source_triage_finalizer_tools(
    update_source_finding_links_handler: ToolHandler,
    update_source_finding_relations_handler: ToolHandler,
    *,
    audit_source_finding_handler: ToolHandler | None = None,
) -> list[AgentTool]:
    tools = [
        update_source_finding_links_tool(update_source_finding_links_handler),
        update_source_finding_relations_tool(update_source_finding_relations_handler),
    ]
    if audit_source_finding_handler is not None:
        tools.insert(0, source_finding_grounding_audit_tool(audit_source_finding_handler))
    return tools


def reading_note_tool(
    handler: ToolHandler,
    *,
    allowed_source_refs: list[str] | None = None,
    allowed_question_ids: list[str] | None = None,
) -> AgentTool:
    """Persist complete agent memory for source ranges already returned to this set agent."""

    return AgentTool(
        name="save_reading_note",
        description=(
            "Persist or replace one durable reading note for the current source. receipt_refs must be exact "
            "handles previously returned by read_source/search_source/open_source_file for this source and set. "
            "A native-file receipt proves only that the file was attached, not that any range or the whole file "
            "was read. This is the semantic checkpoint that lets a fresh worker continue without replaying the "
            "named material. The backend validates handles and identities, not semantic relevance or sufficiency. "
            "This is a provisional checkpoint, not a stopping judgment: describe remaining risk and next checks "
            "without claiming unread material is safe."
        ),
        parameters={
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": (
                        "Existing note_id to replace, or an empty string to create an idempotent note for these "
                        "receipt_refs."
                    ),
                },
                "source_ref": _bounded_id_schema(allowed_source_refs),
                "receipt_refs": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "what_was_read": {"type": "string"},
                "what_was_established": {"type": "string"},
                "boundaries": {"type": "string"},
                "unread_regions": {"type": "string"},
                "unread_risk_assessment": {
                    "type": "string",
                    "description": (
                        "Current risk from unread material. For unfinished reading, state what remains unknown; "
                        "this checkpoint never establishes that unread material is safe."
                    ),
                },
                "question_links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question_id": _bounded_id_schema(allowed_question_ids),
                            "connection": {"type": "string"},
                        },
                        "required": ["question_id", "connection"],
                        "additionalProperties": False,
                    },
                },
                "doubts": {"type": "array", "items": {"type": "string"}},
                "next_checks": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "note_id",
                "source_ref",
                "receipt_refs",
                "what_was_read",
                "what_was_established",
                "boundaries",
                "unread_regions",
                "unread_risk_assessment",
                "question_links",
                "doubts",
                "next_checks",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def source_analysis_tool(
    handler: ToolHandler,
    *,
    allowed_source_refs: list[str] | None = None,
    allowed_question_ids: list[str] | None = None,
) -> AgentTool:
    """Persist one source's meaning; question resolution is a separate decision."""

    parameters = {
        "type": "object",
        "properties": {
            "source_ref": _bounded_id_schema(allowed_source_refs),
            "representation_status": {
                "type": "string",
                "enum": ["readable", "partial", "not_target_content", "unreadable"],
            },
            "representation_reason": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "description": "Explain whether this source representation is readable, partial, not target content, or unreadable.",
            },
            "source_summary": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_summary"],
                "description": "Summarize the source's contribution after reading it.",
            },
            "authority": {
                "type": "string",
                "enum": ["primary", "official", "expert", "secondary", "tertiary", "weak", "unknown"],
            },
            "authority_reason": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "description": "Explain the authority judgment for this source.",
            },
            "uniqueness": {
                "type": "string",
                "enum": ["unique", "complementary", "derivative", "duplicate", "unknown"],
                "description": (
                    "Contribution identity after reading/comparison. duplicate means the same substantive source, "
                    "not unavailable or wrong-target content."
                ),
            },
            "uniqueness_reason": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "description": "Explain whether the source is unique, complementary, derivative, duplicate, or unknown.",
            },
            "duplicate_of_source_ref": {
                **_bounded_optional_id_schema(allowed_source_refs),
                "description": (
                    "Required when uniqueness=duplicate; otherwise empty. Copy the earlier source_ref named by "
                    "read_source or established through source comparison."
                ),
            },
            "limitations": {
                "type": "array",
                "items": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
                    "description": "State one limitation on using this source.",
                },
            },
            "no_findings_reason": {
                "type": "string",
                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["no_findings_reason"],
                "description": (
                    "Explain why actual reading produced no substantive finding. Must be empty when findings exist."
                ),
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_id": {"type": "string"},
                        "statement": {
                            "type": "string",
                            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_statement"],
                            "description": "State exactly one bounded proposition established by this source.",
                        },
                        "support_explanation": {
                            "type": "string",
                            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_support_explanation"],
                            "description": "Explain why the listed passage_refs or evidence_ids support this finding.",
                        },
                        "passage_refs": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                            "description": (
                                "Stable passage refs returned by sequential read_source only. Search refs are "
                                "navigation candidates and are rejected here."
                            ),
                        },
                        "evidence_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                            "description": (
                                "Exact evidence IDs already returned by read_source_window or read_evidence for "
                                "this source. Use these after hydrating a search result."
                            ),
                        },
                        "scope": {
                            "type": "object",
                            "properties": {
                                "actor": {
                                    "type": "string",
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
                                    "description": "Short actor label for the finding scope.",
                                },
                                "period": {
                                    "type": "string",
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
                                    "description": "Short period label for the finding scope.",
                                },
                                "geography": {
                                    "type": "string",
                                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
                                    "description": "Short geography label for the finding scope.",
                                },
                                "qualifiers": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
                                        "description": "Short qualifier label for the finding scope.",
                                    },
                                },
                            },
                            "required": ["actor", "period", "geography", "qualifiers"],
                            "additionalProperties": False,
                        },
                        "limitations": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
                                "description": "State one limitation on this atomic finding.",
                            },
                        },
                        "possible_question_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": _bounded_id_schema(allowed_question_ids),
                            "description": (
                                "Coverage question_id values only. Never pass nested requirement_id values."
                            ),
                        },
                    },
                    "required": [
                        "statement",
                        "support_explanation",
                        "passage_refs",
                        "evidence_ids",
                        "scope",
                        "limitations",
                        "possible_question_ids",
                    ],
                    "additionalProperties": False,
                },
            },
            "reading_basis": reading_basis_schema(),
        },
        "required": [
            "source_ref",
            "representation_status",
            "representation_reason",
            "source_summary",
            "authority",
            "authority_reason",
            "uniqueness",
            "uniqueness_reason",
            "duplicate_of_source_ref",
            "limitations",
            "no_findings_reason",
            "findings",
            "reading_basis",
        ],
        "additionalProperties": False,
    }
    return AgentTool(
        name="save_source_analysis",
        description=(
            "Persist one source's completed analysis and atomic findings. Use passage_refs only from sequential "
            "read_source. A search result is navigation: copy its exact_read_call into read_source_window, then put "
            "the returned evidence_id on the finding. Each finding needs at least one passage_ref or evidence_id. "
            "possible_question_ids accept only question_id values, never requirement_id values, and remain "
            "candidates rather than final links. For an exact duplicate, use uniqueness=duplicate only with "
            "duplicate_of_source_ref copied from backend-confirmed exact_duplicate/exact_duplicate_verified guidance; "
            "save it as readable target content, not as unavailable content. "
            "For a mechanical rejection, retry the complete analysis while changing only the fields named by the "
            "error. After genuinely new reading, replace the complete analysis from the expanded evidence."
        ),
        parameters=parameters,
        handler=handler,
    )


def update_source_finding_links_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="update_source_finding_links",
        description=(
            "Replace only one persisted finding's question/requirement links after comparing it with the complete "
            "question list. The finding text, evidence, scope, and limitations remain unchanged."
        ),
        parameters={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "question_links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question_id": {"type": "string"},
                            "requirement_ids": {"type": "array", "items": {"type": "string"}},
                            "rationale": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["finding_link_rationale"],
                            },
                        },
                        "required": ["question_id", "requirement_ids", "rationale"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["finding_id", "question_links"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def update_source_finding_relations_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="update_source_finding_relations",
        description=(
            "Replace one persisted finding's complete source-relation list after reviewing it against the full "
            "corpus. An empty list removes unsupported relations. Direction is the finding's source_ref -> "
            "related_source_ref; question links and local coverage outcomes remain unchanged."
        ),
        parameters={
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "source_relations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "related_source_ref": {"type": "string"},
                            "relationship": {
                                "type": "string",
                                "enum": [
                                    "duplicate",
                                    "near_duplicate",
                                    "updates",
                                    "contradicts",
                                    "primary_to_secondary",
                                    "background_for",
                                ],
                                "description": (
                                    "Direction is the finding's source_ref -> related_source_ref. "
                                    "Duplicate, near_duplicate, and contradicts are symmetric."
                                ),
                            },
                            "rationale": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS[
                                    "finding_relation_rationale"
                                ],
                            },
                            "evidence_ids": {
                                "type": "array",
                                "minItems": 2,
                                "items": {"type": "string"},
                                "description": (
                                    "Exact grounded non-shell evidence with at least one item from the finding's "
                                    "source and at least one from related_source_ref."
                                ),
                            },
                        },
                        "required": [
                            "related_source_ref",
                            "relationship",
                            "rationale",
                            "evidence_ids",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["finding_id", "source_relations"],
            "additionalProperties": False,
        },
        handler=handler,
    )


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
                        "inspectable text was obtained. Both not_target_content and unreadable require unknown "
                        "source-judgment axes, empty judgment reasons/links, and disposition=deferred."
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
                        "and then requires a coverage outcome before the set can finish. Include every assigned or "
                        "discovered question that this source materially supports, not merely related topics. Must be empty for "
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


def coverage_hint_tool(
    handler: ToolHandler,
    *,
    allowed_source_refs: list[str] | None = None,
    allowed_question_ids: list[str] | None = None,
) -> AgentTool:
    return AgentTool(
        name="save_coverage_hint",
        description=(
            "Persist a source-specific coverage hint for downstream synthesis/research. This is not a question "
            "answer and has no covered/partial/missing status."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hint_id": {
                    "type": "string",
                    "description": "Existing hint_id to replace, or empty to create an idempotent hint.",
                },
                "set_id": {"type": "string"},
                "source_ref": _bounded_id_schema(allowed_source_refs),
                "question_id": _bounded_id_schema(allowed_question_ids),
                "requirement_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "candidate_finding_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "candidate_evidence_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "observed_gap": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "description": "Source-specific uncovered material or limitation; not a corpus-wide absence.",
                },
                "conflict_signal": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                    "description": "Potential conflict this source creates for the question.",
                },
                "notes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_hint_note"],
                    },
                },
            },
            "required": [
                "hint_id",
                "set_id",
                "source_ref",
                "question_id",
                "requirement_ids",
                "candidate_finding_ids",
                "candidate_evidence_ids",
                "observed_gap",
                "conflict_signal",
                "notes",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def question_resolution_tool(
    handler: ToolHandler,
    *,
    allowed_question_ids: list[str] | None = None,
) -> AgentTool:
    return AgentTool(
        name="resolve_triage_question",
        description=(
            "Resolve one question from the current question_workbench snapshot. This write creates "
            "finding-to-requirement links and the local outcome. Read the current finding_catalog first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question_id": _bounded_id_schema(allowed_question_ids),
                "workbench_snapshot_id": {"type": "string", "minLength": 1},
                "requirement_results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement_id": {"type": "string", "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": ["covered", "partial", "missing", "conflicted", "deferred"],
                            },
                            "answer_points": {
                                "type": "array",
                                "description": "Atomic answer propositions for this requirement.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "point": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_summary"],
                                            "description": "One bounded proposition.",
                                        },
                                        "finding_ids": {
                                            "type": "array",
                                            "minItems": 1,
                                            "uniqueItems": True,
                                            "description": "Saved finding ids supporting this point.",
                                            "items": {"type": "string", "minLength": 1},
                                        },
                                    },
                                    "required": ["point", "finding_ids"],
                                    "additionalProperties": False,
                                },
                            },
                            "criterion_match_reason": {
                                "type": "string",
                                "description": "Status justification.",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_closure_basis"],
                            },
                            "unresolved_remainder": {
                                "type": "string",
                                "description": "Unsupported remainder for partial, missing, or conflicted.",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                            },
                            "conflict_summary": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                                "description": "Summarize the unresolved conflict for this requirement.",
                            },
                            "deferred_reason": {
                                "type": "string",
                                "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                                "description": "Explain why this requirement is deferred.",
                            },
                        },
                        "required": [
                            "requirement_id",
                            "status",
                            "answer_points",
                            "criterion_match_reason",
                            "unresolved_remainder",
                            "conflict_summary",
                            "deferred_reason",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "question_id",
                "workbench_snapshot_id",
                "requirement_results",
            ],
            "additionalProperties": False,
        },
        handler=handler,
    )


def coverage_outcome_tool(handler: ToolHandler) -> AgentTool:
    return AgentTool(
        name="upsert_coverage_outcome",
        description=(
            "Persist or replace one current-set question outcome. It must reference linked findings and contain "
            "one result per assigned or newly linked requirement; source and evidence lineage is derived by the "
            "backend. A lineage error names the exact question, requirement, finding, owner source, and the "
            "save_source_analysis correction needed before retrying."
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
                        "but unsupported; deferred means the checked representation prevents evaluation. When "
                        "requirement_results are present, aggregate exactly: conflicted if any requirement is "
                        "conflicted; covered if all are covered; missing if all are missing; deferred if at least one "
                        "is deferred and every other result is missing or deferred; partial for every other non-empty "
                        "combination."
                    ),
                },
                "requirement_results": {
                    "type": "array",
                    "description": (
                        "One result for each requirement assigned to this set or linked by one of its findings."
                    ),
                    "items": _requirement_result_schema(),
                },
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
                    "description": "Summarize the conflict for this outcome.",
                },
                "deferred_reason": {
                    "type": "string",
                    "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                    "description": "Explain why this outcome is deferred.",
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
                "requirement_results",
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
            "Persist or replace one run-wide question synthesis after local outcomes are current. Include one "
            "result per answer requirement; the backend derives source and evidence lineage."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "requirement_results": {
                    "type": "array",
                    "description": "Run-wide result for every semantic answer requirement of the question.",
                    "items": _requirement_result_schema(allow_global_next_action=True),
                },
                "status": {
                    "type": "string",
                    "enum": ["covered", "partial", "missing", "conflicted", "deferred"],
                    "description": (
                        "partial requires both established orientation and a concrete remainder; missing means checked "
                        "but unsupported; deferred means the checked representation prevents evaluation. When "
                        "requirement_results are present, aggregate exactly: conflicted if any requirement is "
                        "conflicted; covered if all are covered; missing if all are missing; deferred if at least one "
                        "is deferred and every other result is missing or deferred; partial for every other non-empty "
                        "combination."
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
                        "research=relevant current-corpus material exists but needs deeper reading/comparison; "
                        "source_acquisition=the complete packet stream shows needed material is absent from the "
                        "current corpus; omit=do not carry forward; none=no further work. For structured questions, "
                        "the backend normalizes this compatibility field from requirement_results[].next_action using "
                        "priority source_acquisition > research > omit > none; the supplied value is ignored. For "
                        "legacy questions without answer requirements, the supplied value is authoritative."
                    ),
                },
            },
            "required": [
                "question_id",
                "requirement_results",
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


def _requirement_result_schema(
    *,
    allow_global_next_action: bool = False,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "requirement_id": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["covered", "partial", "missing", "conflicted", "deferred"],
            "description": (
                "covered=summary+findings only; partial=summary+findings+missing_detail; "
                "missing=missing_detail only; conflicted=findings+conflict_summary; "
                "deferred=deferred_reason only. Keep all non-applicable text fields empty."
            ),
        },
        "finding_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Required for covered/partial/conflicted; empty for missing/deferred.",
        },
        "summary": {
            "type": "string",
            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_summary"],
            "description": "Requirement-level established summary; required for covered/partial and empty for missing/deferred.",
        },
        "closure_basis": {
            "type": "string",
            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_closure_basis"],
            "description": (
                "For covered, explain specifically why the linked findings satisfy this requirement's persisted "
                "completion_criterion. For partial/conflicted, state only what the findings establish. Empty for "
                "missing/deferred."
            ),
        },
        "remaining_uncertainty": {
            "type": "string",
            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_uncertainty"],
            "description": (
                "Material uncertainty left after applying the linked findings. Empty only when none remains or "
                "when missing/deferred fields already fully state the absence."
            ),
        },
        "missing_detail": {
            "type": "string",
            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
            "description": "Missing detail; required for partial/missing and empty otherwise.",
        },
        "conflict_summary": {
            "type": "string",
            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
            "description": "Conflict summary; required only for conflicted.",
        },
        "deferred_reason": {
            "type": "string",
            "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
            "description": "Deferred reason; required only for deferred.",
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
    required = [
        "requirement_id",
        "status",
        "finding_ids",
        "summary",
        "closure_basis",
        "remaining_uncertainty",
        "missing_detail",
        "conflict_summary",
        "deferred_reason",
        "confidence",
    ]
    if allow_global_next_action:
        properties["next_action"] = {
            "type": "string",
            "enum": ["research", "source_acquisition", "omit", "none"],
            "description": (
                "Authoritative downstream route for this answer requirement: research=deeper reading or comparison "
                "of relevant current-corpus material; source_acquisition=needed material is absent from the completed "
                "corpus review; omit=do not carry this part forward; none=this part needs no further work."
            ),
        }
        required.append("next_action")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }

def _bounded_id_schema(values: list[str] | None) -> dict[str, Any]:
    allowed = list(dict.fromkeys(str(value) for value in values or [] if str(value)))
    return {
        "type": "string",
        "minLength": 1,
        **({"enum": allowed} if allowed else {}),
    }


def _bounded_optional_id_schema(values: list[str] | None) -> dict[str, Any]:
    allowed = list(dict.fromkeys(str(value) for value in values or [] if str(value)))
    return {
        "type": "string",
        **({"enum": ["", *allowed]} if allowed else {}),
    }


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
                    "enum": ["mapped", "deferred"],
                    "description": (
                        "Both statuses require completed source analyses. Use mapped when at least one source was "
                        "mapped; keep deferred_reason empty and list any individually representation-deferred "
                        "sources in deferred_sources. Use deferred only when every source assessment is deferred; "
                        "provide a set-level deferred_reason."
                    ),
                },
                "summary": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["findings_summary"]},
                "deferred_reason": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"]},
                "deferred_sources": {
                    "type": "array",
                    "description": (
                        "Per-source representation deferrals. Each listed source must belong to this set and already "
                        "have disposition=deferred; mixed sets still finish as mapped."
                    ),
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
