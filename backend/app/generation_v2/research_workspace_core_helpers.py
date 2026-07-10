from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.generation_v2.contracts.research import StoryWeight
from app.generation_v2.research_kinds import ALLOWED_RESEARCH_ATOM_KINDS, normalize_research_atom_kind
from app.generation_v2.research_workspace_contracts import RESEARCH_ATOM_STORY_WEIGHTS
from app.generation_v2.research_workspace_gap_helpers import normalized_question_key
from app.generation_v2.schemas import SourceMap


def source_map_signature(source_map: SourceMap, source_inventory: SourceMap) -> dict[str, Any]:
    source_map_payload = source_map.model_dump(mode="json")
    inventory_payload = [
        {
            "source_id": card.source_id,
            "source_version_ids": card.source_version_ids,
            "title": card.title,
            "domain": card.domain,
            "source_type": card.source_type,
            "summary": card.summary,
            "useful_for": card.useful_for,
            "gaps_or_cautions": card.gaps_or_cautions,
            "coverage_status": card.coverage_status,
            "evidence_ids": card.evidence_ids,
        }
        for card in source_inventory.source_cards
    ]
    return {
        "schema_version": "research_source_map_signature.v2",
        "source_map_hash": stable_hash(source_map_payload),
        "source_inventory_hash": stable_hash(inventory_payload),
        "inventory_source_count": len(source_inventory.source_cards),
        "source_map_source_count": len(source_map.source_cards),
        "quote_count": len(source_map.quote_cards),
        "cluster_count": len(source_map.source_triage.source_clusters),
        "relationship_count": len(source_map.source_triage.source_relationships),
        "evidence_count": len(source_map.evidence_ids),
    }


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(requested: str, label: str, index: int) -> str:
    if requested:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in requested)[:56].strip("_") or f"item_{index}"
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())[:48].strip("_")
    return slug or f"item_{index}"


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def dedupe_normalized(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = normalized_question_key(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def active_atom_drafts(values: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in values
        if isinstance(item, dict) and not str(item.get("merged_into") or "").strip() and str(item.get("status") or "") != "merged"
    ]


def active_job_atom_count(workspace: dict[str, Any], job: dict[str, Any]) -> int:
    job_atom_ids = {str(item) for item in job.get("atom_ids") or [] if str(item).strip()}
    if not job_atom_ids:
        return 0
    return len(
        [
            draft
            for draft in active_atom_drafts(workspace.get("atom_drafts") or [])
            if str(draft.get("atom_id") or "") in job_atom_ids
        ]
    )


def atom_kind(value: str) -> str | None:
    text = normalize_research_atom_kind(value, default="")
    return text if text in ALLOWED_RESEARCH_ATOM_KINDS else None


def coerce_atom_kind(value: str) -> str:
    return atom_kind(value) or "open_question"


def story_weight(value: str, *, kind: str = "") -> str:
    text = str(value or "").strip()
    if text in RESEARCH_ATOM_STORY_WEIGHTS:
        return text
    return StoryWeight.SUPPORTING.value


def coverage_status(value: str) -> str:
    text = str(value or "").strip()
    return text if text in {"ready", "partial", "weak"} else "partial"


def confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def now() -> str:
    return datetime.utcnow().isoformat()


__all__ = [
    "active_atom_drafts",
    "active_job_atom_count",
    "atom_kind",
    "coerce_atom_kind",
    "confidence",
    "coverage_status",
    "dedupe",
    "dedupe_normalized",
    "now",
    "parse_dt",
    "source_map_signature",
    "stable_hash",
    "stable_id",
    "story_weight",
]
