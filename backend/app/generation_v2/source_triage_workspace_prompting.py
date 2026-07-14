from __future__ import annotations

from typing import Any

from app.generation_v2.source_triage_workspace_state import (
    normalize_workspace as _normalize_workspace,
    source_set_has_unresolved_refs as _source_set_has_unresolved_refs,
    workspace_set as _workspace_set,
)


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = str(value or "")
    return {
        f"{name}_preview": text[:limit],
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _observation_previews(source_set: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    observations = [item for item in source_set.get("observations") or [] if isinstance(item, dict)]
    return [
        {
            "source_refs": item.get("source_refs") or [],
            "evidence_ids": (item.get("evidence_ids") or [])[:8],
            "evidence_count": len(item.get("evidence_ids") or []),
            "evidence_omitted_count": max(0, len(item.get("evidence_ids") or []) - 8),
            **_preview_fields("note", item.get("note") or "", 500),
            "pending_question_previews": [
                _preview_fields("text", question, 220)
                for question in (item.get("pending_questions") or [])[:8]
            ],
            "pending_question_count": len(item.get("pending_questions") or []),
        }
        for item in observations[-limit:]
    ]


def _written_action_previews(source_set: dict[str, Any], *, limit: int = 16) -> list[dict[str, Any]]:
    written_actions = [item for item in source_set.get("written_actions") or [] if isinstance(item, dict)]
    return [
        {
            "tool_name": item.get("tool_name"),
            "source_refs": item.get("source_refs") or [],
            "evidence_ids": (item.get("evidence_ids") or [])[:8],
            "evidence_count": len(item.get("evidence_ids") or []),
            "evidence_omitted_count": max(0, len(item.get("evidence_ids") or []) - 8),
            "created_at": item.get("created_at"),
        }
        for item in written_actions[-limit:]
    ]


def _capsule_prompt_preview(capsule: dict[str, Any]) -> dict[str, Any]:
    outline = list(capsule.get("outline") or [])
    windows = [item for item in capsule.get("windows") or [] if isinstance(item, dict)]
    warnings = list(capsule.get("readability_warnings") or [])
    return {
        "source_ref": capsule.get("source_ref"),
        **_preview_fields("title", capsule.get("title") or "", 180),
        "domain": capsule.get("domain"),
        "source_type": capsule.get("source_type"),
        "published_at": capsule.get("published_at"),
        "coverage_status": capsule.get("coverage_status"),
        "readable": bool(capsule.get("readable")),
        **_preview_fields("summary_hint", capsule.get("summary_hint") or "", 180),
        "outline_previews": [_preview_fields("text", item, 100) for item in outline[:4]],
        "outline_count": len(outline),
        "window_previews": [
            {
                **_preview_fields("locator", item.get("locator") or "", 100),
                "locator_type": item.get("locator_type"),
                **_preview_fields("text", item.get("preview_text") or "", 300),
            }
            for item in windows[:2]
        ],
        "window_count": len(windows),
        "readability_warning_previews": [
            _preview_fields("text", item, 300) for item in warnings[:4]
        ],
        "readability_warning_count": len(warnings),
    }


def workspace_summary_for_prompt(
    workspace: dict[str, Any],
    *,
    max_sets: int = 30,
    set_cursor: int = 0,
    max_deferred_sources: int = 20,
    deferred_source_cursor: int = 0,
) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    source_sets = list(workspace.get("source_sets") or [])
    set_page = source_sets[set_cursor : set_cursor + max_sets]
    deferred_sources = [
        item for item in workspace.get("deferred_sources") or [] if isinstance(item, dict)
    ]
    deferred_page = deferred_sources[
        deferred_source_cursor : deferred_source_cursor + max_deferred_sources
    ]
    return {
        "status": workspace.get("status"),
        **_preview_fields("plan_summary", workspace.get("plan_summary") or "", 500),
        "source_count": len(workspace.get("source_order") or []),
        "source_set_count": len(workspace.get("source_sets") or []),
        "current_set": workspace.get("current_set"),
        "processed_sets": [
            {
                "set_id": item.get("set_id"),
                "status": item.get("status"),
                **_preview_fields("summary", item.get("summary") or "", 500),
            }
            for item in workspace.get("processed_sets") or []
            if isinstance(item, dict)
        ],
        "mapped_sources": workspace.get("mapped_sources") or [],
        "deferred_sources": [
            {
                "source_ref": item.get("source_ref"),
                **_preview_fields("reason", item.get("reason") or "", 400),
            }
            for item in deferred_page
        ],
        "deferred_source_count": len(deferred_sources),
        "deferred_sources_page": {
            "total_count": len(deferred_sources),
            "returned_count": len(deferred_page),
            "omitted_count": max(
                0,
                len(deferred_sources) - (deferred_source_cursor + len(deferred_page)),
            ),
            "cursor": str(deferred_source_cursor),
            "next_cursor": (
                str(deferred_source_cursor + len(deferred_page))
                if deferred_source_cursor + len(deferred_page) < len(deferred_sources)
                else ""
            ),
        },
        "open_question_previews": [
            _preview_fields("text", item, 400) for item in workspace.get("open_questions") or []
        ],
        "source_sets": [
            {
                "set_id": item.get("set_id"),
                **_preview_fields("label", item.get("label") or "", 180),
                **_preview_fields("purpose", item.get("purpose") or "", 400),
                "source_refs": item.get("source_refs") or [],
                "status": item.get("status"),
                "priority": item.get("priority"),
                **_preview_fields("findings_summary", item.get("findings_summary") or "", 600),
                "observation_count": len(item.get("observations") or []),
                "written_action_count": len(item.get("written_actions") or []),
                **_preview_fields("deferred_reason", item.get("deferred_reason") or "", 400),
            }
            for item in set_page
        ],
        "source_sets_page": {
            "total_count": len(source_sets),
            "returned_count": len(set_page),
            "omitted_count": max(0, len(source_sets) - (set_cursor + len(set_page))),
            "cursor": str(set_cursor),
            "next_cursor": str(set_cursor + len(set_page)) if set_cursor + len(set_page) < len(source_sets) else "",
        },
    }


def checkpoint_workspace_payload(
    workspace: dict[str, Any], *, max_sets: int = 40
) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    summary = workspace_summary_for_prompt(workspace, max_sets=max_sets)
    current_set_pointer = workspace.get("current_set") or {}
    current_set_id = str(current_set_pointer.get("set_id") or "")
    current_set = _workspace_set(workspace, current_set_id) if current_set_id else None
    observations = list((current_set or {}).get("observations") or [])
    observation_previews = _observation_previews(current_set or {})
    written_actions = list((current_set or {}).get("written_actions") or [])
    written_action_previews = _written_action_previews(current_set or {})
    capsules = workspace.get("source_capsules") or {}
    current_capsule_previews = [
        _capsule_prompt_preview(capsule)
        for source_ref in (current_set or {}).get("source_refs") or []
        if isinstance((capsule := capsules.get(source_ref)), dict)
    ]
    current_set_summary = None
    if current_set is not None:
        current_set_summary = {
            "set_id": current_set.get("set_id"),
            "status": current_set.get("status"),
            "source_refs": current_set.get("source_refs") or [],
            "priority": current_set.get("priority"),
            "started_at": current_set_pointer.get("started_at"),
            **_preview_fields("label", current_set.get("label") or "", 180),
            **_preview_fields("purpose", current_set.get("purpose") or "", 400),
            **_preview_fields("rationale", current_set.get("rationale") or "", 500),
            "question_previews": [
                _preview_fields("text", item, 280) for item in current_set.get("questions") or []
            ],
            "observation_count": len(observations),
            "observation_preview_count": len(observation_previews),
            "observation_omitted_count": max(0, len(observations) - len(observation_previews)),
            "written_action_count": len(written_actions),
            "written_action_preview_count": len(written_action_previews),
            "written_action_omitted_count": max(0, len(written_actions) - len(written_action_previews)),
        }
    return {
        "status": summary.get("status"),
        "source_count": summary.get("source_count"),
        "source_set_count": summary.get("source_set_count"),
        "processed_set_count": len(workspace.get("processed_sets") or []),
        "mapped_source_count": len(workspace.get("mapped_sources") or []),
        "deferred_source_count": len(workspace.get("deferred_sources") or []),
        "open_question_count": len(workspace.get("open_questions") or []),
        "source_sets": summary.get("source_sets") or [],
        "source_sets_page": summary.get("source_sets_page") or {},
        "current_set": current_set_summary,
        "current_set_observation_previews": observation_previews,
        "current_set_written_action_previews": written_action_previews,
        "current_set_capsule_previews": current_capsule_previews,
    }


def unresolved_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "in_progress", "needs_repair"}
        or _source_set_has_unresolved_refs(workspace, item)
    ]


def unresolved_source_set_summary(workspace: dict[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
    return [
        {
            "set_id": item.get("set_id"),
            "label": item.get("label"),
            "status": item.get("status"),
            "source_refs": item.get("source_refs") or [],
            "deferred_reason": item.get("deferred_reason") or "",
        }
        for item in (unresolved_source_sets(workspace)[:limit] if limit is not None else unresolved_source_sets(workspace))
    ]


def source_set_prompt_payload(workspace: dict[str, Any], set_id: str) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    source_set = _workspace_set(workspace, set_id) or {}
    capsules = workspace.get("source_capsules") or {}
    observations = list(source_set.get("observations") or [])
    observation_previews = _observation_previews(source_set)
    written_actions = list(source_set.get("written_actions") or [])
    written_action_previews = _written_action_previews(source_set)
    capsule_previews = [
        _capsule_prompt_preview(capsule)
        for ref in source_set.get("source_refs") or []
        if isinstance((capsule := capsules.get(ref)), dict)
    ]
    return {
        "set": {
            "set_id": source_set.get("set_id"),
            **_preview_fields("label", source_set.get("label") or "", 180),
            **_preview_fields("purpose", source_set.get("purpose") or "", 400),
            "source_refs": source_set.get("source_refs") or [],
            **_preview_fields("rationale", source_set.get("rationale") or "", 500),
            "question_previews": [
                _preview_fields("text", question, 280)
                for question in source_set.get("questions") or []
            ],
            "question_count": len(source_set.get("questions") or []),
            "priority": source_set.get("priority"),
            "observation_previews": observation_previews,
            "observation_count": len(observations),
            "observation_omitted_count": max(0, len(observations) - len(observation_previews)),
            "written_action_previews": written_action_previews,
            "written_action_count": len(written_actions),
            "written_action_omitted_count": max(0, len(written_actions) - len(written_action_previews)),
            "evidence_count": len(source_set.get("evidence_ids") or []),
        },
        "capsule_previews": capsule_previews,
        "capsule_count": len(capsule_previews),
        "workspace_progress": workspace_summary_for_prompt(workspace, max_sets=40),
    }


def unprocessed_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "needs_repair"}
        or _source_set_has_unresolved_refs(workspace, item)
    ]


__all__ = [
    "checkpoint_workspace_payload",
    "source_set_prompt_payload",
    "unprocessed_source_sets",
    "unresolved_source_set_summary",
    "unresolved_source_sets",
    "workspace_summary_for_prompt",
]
