from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.generation_v2.runtime import AgentTool
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_CAPSULE_PAGE_MAX_LIMIT,
    TRIAGE_PENDING_QUESTION_LIMIT,
    TRIAGE_QUESTION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)


ToolHandler = Callable[[dict[str, Any]], Any]


def source_triage_planner_tools(
    list_source_capsules_handler: ToolHandler,
    upsert_triage_source_set_handler: ToolHandler,
    finalize_triage_workspace_plan_handler: ToolHandler,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="list_source_capsules",
            description="List compact orientation capsules for source_refs. Capsules contain neutral readable hints, not source-role decisions.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": TRIAGE_CAPSULE_PAGE_MAX_LIMIT},
                    "cursor": {"type": "string"},
                },
                "required": ["limit", "cursor"],
            },
            handler=list_source_capsules_handler,
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
                    "questions": {
                        "type": "array",
                        "maxItems": TRIAGE_QUESTION_LIMIT,
                        "items": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["question"]},
                    },
                    "priority": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["priority"]},
                    "repair": {"type": "boolean"},
                },
                "required": ["label", "purpose", "source_refs", "rationale", "questions", "priority"],
            },
            handler=upsert_triage_source_set_handler,
        ),
        AgentTool(
            name="finalize_triage_workspace_plan",
            description="Mark the source-set plan complete. Every source_ref must be in a set or explicitly deferred.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["plan_summary"]},
                    "deferred_sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_refs": {"type": "array", "items": {"type": "string"}},
                                "reason": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"]},
                            },
                            "required": ["source_refs", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "deferred_reason": {"type": "string", "maxLength": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"]},
                },
                "required": ["summary", "deferred_sources", "deferred_reason"],
            },
            handler=finalize_triage_workspace_plan_handler,
        ),
    ]


def source_triage_set_tools(
    record_triage_observation_handler: ToolHandler,
    finish_triage_source_set_handler: ToolHandler,
) -> list[AgentTool]:
    return [
        AgentTool(
            name="record_triage_observation",
            description="Persist a compact intermediate finding for the current source set before reading more or crossing a checkpoint boundary.",
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
        finish_triage_source_set_tool(
            finish_triage_source_set_handler,
            description="Finish the current source set after writing source cards/clusters/relationships/gaps or explicitly deferring it.",
        ),
    ]


def source_triage_finalizer_tools(finish_triage_source_set_handler: ToolHandler) -> list[AgentTool]:
    return [
        finish_triage_source_set_tool(
            finish_triage_source_set_handler,
            description="Mark a repair source set as deferred/needs_repair when finalization cannot proceed.",
        )
    ]


def finish_triage_source_set_tool(handler: ToolHandler, *, description: str) -> AgentTool:
    return AgentTool(
        name="finish_triage_source_set",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "set_id": {"type": "string"},
                "status": {"type": "string", "enum": ["mapped", "deferred", "needs_repair"]},
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
    )
