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
