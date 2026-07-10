from __future__ import annotations

from typing import Any

from app.generation_v2.source_triage_workspace_state import (
    normalize_workspace as _normalize_workspace,
    workspace_set as _workspace_set,
)


def workspace_summary_for_prompt(workspace: dict[str, Any], *, max_sets: int = 30) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    return {
        "status": workspace.get("status"),
        "source_count": len(workspace.get("source_order") or []),
        "source_set_count": len(workspace.get("source_sets") or []),
        "current_set": workspace.get("current_set"),
        "processed_sets": workspace.get("processed_sets") or [],
        "mapped_sources": workspace.get("mapped_sources") or [],
        "deferred_sources": workspace.get("deferred_sources") or [],
        "open_questions": workspace.get("open_questions") or [],
        "source_sets": [
            {
                "set_id": item.get("set_id"),
                "label": item.get("label"),
                "purpose": item.get("purpose"),
                "source_refs": item.get("source_refs") or [],
                "status": item.get("status"),
                "priority": item.get("priority"),
                "findings_summary": item.get("findings_summary"),
                "observation_count": len(item.get("observations") or []),
                "written_action_count": len(item.get("written_actions") or []),
                "deferred_reason": item.get("deferred_reason"),
            }
            for item in (workspace.get("source_sets") or [])[:max_sets]
        ],
    }


def unresolved_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "in_progress", "needs_repair"}
    ]


def unresolved_source_set_summary(workspace: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "set_id": item.get("set_id"),
            "label": item.get("label"),
            "status": item.get("status"),
            "source_refs": item.get("source_refs") or [],
            "deferred_reason": item.get("deferred_reason") or "",
        }
        for item in unresolved_source_sets(workspace)[:limit]
    ]


def source_set_prompt_payload(workspace: dict[str, Any], set_id: str) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    source_set = _workspace_set(workspace, set_id) or {}
    capsules = workspace.get("source_capsules") or {}
    return {
        "set": {
            "set_id": source_set.get("set_id"),
            "label": source_set.get("label"),
            "purpose": source_set.get("purpose"),
            "source_refs": source_set.get("source_refs") or [],
            "rationale": source_set.get("rationale"),
            "questions": source_set.get("questions") or [],
            "priority": source_set.get("priority"),
            "observations": source_set.get("observations") or [],
            "written_actions": source_set.get("written_actions") or [],
        },
        "capsules": [capsules.get(ref) for ref in source_set.get("source_refs") or [] if capsules.get(ref)],
        "workspace_progress": workspace_summary_for_prompt(workspace, max_sets=40),
    }


def unprocessed_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "needs_repair"}
    ]


__all__ = [
    "source_set_prompt_payload",
    "unprocessed_source_sets",
    "unresolved_source_set_summary",
    "unresolved_source_sets",
    "workspace_summary_for_prompt",
]
