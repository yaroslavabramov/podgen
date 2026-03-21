"""Shared dialogue helpers for phase 6 writing/audit passes."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select

from app.models.episode_manifest import EpisodeManifest
from app.models.script_segment import ScriptSegment

_SPEAKER_TRANSLATION = str.maketrans({
    "А": "A",
    "а": "a",
    "Б": "B",
    "б": "b",
})


def normalize_speaker_label(value: object) -> str:
    if not isinstance(value, str):
        return "A"

    text = value.strip()
    if not text:
        return "A"

    translated = text.translate(_SPEAKER_TRANSLATION).upper()
    if translated in {"A", "B"}:
        return translated

    match = re.fullmatch(r"(?:HOST|SPEAKER)?\s*([AB])", translated)
    if match:
        return match.group(1)

    return "A"


def extract_dialogue_texts(dialogue_json: object) -> list[str]:
    ordered: list[str] = []
    if not isinstance(dialogue_json, list):
        return ordered
    for line in dialogue_json:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if not isinstance(text, str):
            continue
        cleaned = " ".join(text.strip().split())
        if cleaned:
            ordered.append(cleaned)
    return ordered


def extract_dialogue_claim_ids(dialogue_json: object) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    if not isinstance(dialogue_json, list):
        return ordered
    for line in dialogue_json:
        if not isinstance(line, dict):
            continue
        claim_ids = line.get("claim_ids")
        if not isinstance(claim_ids, list):
            continue
        for claim_id in claim_ids:
            if not isinstance(claim_id, str) or not claim_id or claim_id in seen:
                continue
            seen.add(claim_id)
            ordered.append(claim_id)
    return ordered


def normalize_dialogue_output(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    dialogue: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        cleaned_text = " ".join(text.strip().split())
        if not cleaned_text:
            continue
        claim_ids = item.get("claim_ids")
        normalized_claim_ids: list[str] = []
        seen_claim_ids: set[str] = set()
        if isinstance(claim_ids, list):
            for claim_id in claim_ids:
                if not isinstance(claim_id, str):
                    continue
                trimmed = claim_id.strip()
                if not trimmed or trimmed in seen_claim_ids:
                    continue
                seen_claim_ids.add(trimmed)
                normalized_claim_ids.append(trimmed)
        dialogue.append(
            {
                "speaker": normalize_speaker_label(item.get("speaker")),
                "text": cleaned_text,
                "claim_ids": normalized_claim_ids,
            }
        )
    return dialogue or None


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def segment_audit_finding(segment: ScriptSegment, key: str) -> dict[str, Any]:
    if not isinstance(segment.audit_findings, dict):
        return {}
    current = segment.audit_findings.get(key)
    return current if isinstance(current, dict) else {}


def set_segment_audit_finding(segment: ScriptSegment, key: str, payload: dict[str, Any]) -> None:
    findings = dict(segment.audit_findings or {})
    findings[key] = payload
    segment.audit_findings = findings


def _uuid_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        trimmed = item.strip()
        if not trimmed:
            continue
        try:
            normalized = str(uuid.UUID(trimmed))
        except (TypeError, ValueError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def scene_primary_claim_ids(scene: dict[str, Any]) -> list[str]:
    primary_claim_ids = _uuid_list(scene.get("primary_claim_ids"))
    if primary_claim_ids:
        return primary_claim_ids
    return _uuid_list(scene.get("claim_ids"))


def scene_callback_claim_ids(scene: dict[str, Any]) -> list[str]:
    return _uuid_list(scene.get("callback_claim_ids"))


def scene_manifest_claim_ids(scene: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for source in (
        scene_primary_claim_ids(scene),
        scene_callback_claim_ids(scene),
        _uuid_list(scene.get("claim_ids")),
    ):
        for claim_id in source:
            if claim_id in seen:
                continue
            seen.add(claim_id)
            ordered.append(claim_id)
    return ordered


def normalize_scene_job_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def build_manifest_scene_metadata_lookup(acts_json: object) -> dict[tuple[int, int], dict[str, Any]]:
    if not isinstance(acts_json, list):
        return {}

    lookup: dict[tuple[int, int], dict[str, Any]] = {}
    for act_idx, act in enumerate(acts_json, start=1):
        if not isinstance(act, dict):
            continue
        try:
            act_number = int(act.get("act_number") or act_idx)
        except (TypeError, ValueError):
            act_number = act_idx
        beat_sheet = act.get("beat_sheet", [])
        if not isinstance(beat_sheet, list):
            continue
        for scene_idx, scene in enumerate(beat_sheet, start=1):
            if not isinstance(scene, dict):
                continue
            try:
                scene_number = int(scene.get("scene") or scene_idx)
            except (TypeError, ValueError):
                scene_number = scene_idx

            description = scene.get("description")
            scene_type = scene.get("type")
            lookup[(act_number, scene_number)] = {
                "scene_job": normalize_scene_job_value(scene.get("scene_job")),
                "scene_type": scene_type.strip().lower() if isinstance(scene_type, str) else "",
                "description": " ".join(description.strip().split()) if isinstance(description, str) else "",
                "claim_ids": scene_manifest_claim_ids(scene),
                "primary_claim_ids": scene_primary_claim_ids(scene),
                "callback_claim_ids": scene_callback_claim_ids(scene),
            }
    return lookup


async def load_manifest_scene_metadata_lookup(db, manifest_id: str | None) -> dict[tuple[int, int], dict[str, Any]]:
    if not manifest_id:
        return {}
    try:
        manifest_uuid = uuid.UUID(manifest_id)
    except (TypeError, ValueError):
        return {}

    result = await db.execute(select(EpisodeManifest).where(EpisodeManifest.id == manifest_uuid))
    manifest = result.scalar_one_or_none()
    if manifest is None:
        return {}
    return build_manifest_scene_metadata_lookup(manifest.acts_json)
