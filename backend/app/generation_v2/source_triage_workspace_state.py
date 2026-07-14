from __future__ import annotations

from datetime import datetime
from typing import Any

WORKSPACE_SCHEMA_VERSION = "source_triage_workspace.v1"


def normalize_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(workspace or {})
    workspace.setdefault("schema_version", WORKSPACE_SCHEMA_VERSION)
    workspace.setdefault("source_order", [])
    workspace.setdefault("source_capsules", {})
    workspace.setdefault("source_sets", [])
    workspace.setdefault("processed_sets", [])
    workspace.setdefault("deferred_sources", [])
    workspace.setdefault("mapped_sources", [])
    workspace.setdefault("open_questions", [])
    workspace.setdefault("repair_operation", None)
    return workspace


def workspace_set(workspace: dict[str, Any], set_id: str) -> dict[str, Any] | None:
    for item in workspace.get("source_sets") or []:
        if item.get("set_id") == set_id:
            return dict(item)
    return None


def replace_workspace_set(workspace: dict[str, Any], source_set: dict[str, Any]) -> None:
    set_id = source_set.get("set_id")
    workspace["source_sets"] = [
        source_set if item.get("set_id") == set_id else item for item in workspace.get("source_sets") or []
    ]


def stable_set_id(requested: str, label: str, index: int) -> str:
    if requested:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in requested)[:48].strip("_") or f"set_{index}"
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())[:44].strip("_")
    return slug or f"set_{index}"


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def merge_deferred(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ref = {str(item.get("source_ref")): dict(item) for item in existing if item.get("source_ref")}
    for item in new_items:
        ref = str(item.get("source_ref") or "")
        if ref:
            by_ref[ref] = {"source_ref": ref, "reason": str(item.get("reason") or "")}
    return list(by_ref.values())


def now() -> str:
    return datetime.utcnow().isoformat()


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def source_set_covered_refs(source_set: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for item in [*(source_set.get("observations") or []), *(source_set.get("written_actions") or [])]:
        if isinstance(item, dict):
            refs.extend(str(ref) for ref in item.get("source_refs") or [])
    return dedupe(refs)


def unresolved_source_refs(workspace: dict[str, Any]) -> list[str]:
    current = normalize_workspace(workspace)
    resolved = set(str(item) for item in current.get("mapped_sources") or [])
    resolved.update(
        str(item.get("source_ref") or "")
        for item in current.get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    )
    for source_set in current.get("source_sets") or []:
        if source_set.get("status") == "mapped":
            resolved.update(source_set_covered_refs(source_set))
        elif source_set.get("status") == "deferred" and str(source_set.get("deferred_reason") or "").strip():
            resolved.update(str(item) for item in source_set.get("source_refs") or [])
    return [str(ref) for ref in current.get("source_order") or [] if str(ref) not in resolved]


def source_set_has_unresolved_refs(workspace: dict[str, Any], source_set: dict[str, Any]) -> bool:
    if source_set.get("status") != "mapped":
        return False
    covered = set(source_set_covered_refs(source_set))
    deferred = {
        str(item.get("source_ref") or "")
        for item in normalize_workspace(workspace).get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    }
    return any(str(ref) not in covered and str(ref) not in deferred for ref in source_set.get("source_refs") or [])
