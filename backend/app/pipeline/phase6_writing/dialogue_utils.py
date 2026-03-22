"""Shared dialogue helpers for phase 6 writing/audit passes."""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
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


def _normalize_speaker_alias_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.translate(_SPEAKER_TRANSLATION).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" \t\r\n:;,.!?-–—()[]{}\"'«»")
    return text.casefold()


def _register_speaker_alias(
    aliases: dict[str, str],
    collisions: set[str],
    *,
    candidate: object,
    label: str,
) -> None:
    key = _normalize_speaker_alias_key(candidate)
    if not key or key in {"a", "b"}:
        return
    existing = aliases.get(key)
    if existing and existing != label:
        collisions.add(key)
        aliases.pop(key, None)
        return
    if key not in collisions:
        aliases[key] = label


def build_speaker_aliases(hosts: object) -> dict[str, str]:
    if not isinstance(hosts, list):
        return {}

    aliases: dict[str, str] = {}
    collisions: set[str] = set()
    first_token_candidates: list[tuple[str, str]] = []

    for idx, host in enumerate(hosts[:2]):
        if not isinstance(host, dict):
            continue
        label = "A" if idx == 0 else "B"
        name = host.get("name")
        role = host.get("role")
        _register_speaker_alias(aliases, collisions, candidate=name, label=label)
        _register_speaker_alias(aliases, collisions, candidate=role, label=label)

        if isinstance(name, str):
            tokens = re.findall(r"\S+", name.strip())
            if tokens:
                first_token_candidates.append((tokens[0], label))

    unique_first_tokens: dict[str, str] = {}
    duplicate_first_tokens: set[str] = set()
    for token, label in first_token_candidates:
        key = _normalize_speaker_alias_key(token)
        if not key or key in {"a", "b"}:
            continue
        existing = unique_first_tokens.get(key)
        if existing and existing != label:
            duplicate_first_tokens.add(key)
            unique_first_tokens.pop(key, None)
            continue
        if key not in duplicate_first_tokens:
            unique_first_tokens[key] = label

    for key, label in unique_first_tokens.items():
        if key in collisions:
            continue
        aliases.setdefault(key, label)

    return aliases


def _lookup_speaker_alias(value: str, speaker_aliases: dict[str, str] | None) -> str | None:
    if not speaker_aliases:
        return None
    normalized_value = _normalize_speaker_alias_key(value)
    if not normalized_value:
        return None

    direct = speaker_aliases.get(normalized_value)
    if direct in {"A", "B"}:
        return direct

    raw_direct = speaker_aliases.get(value.strip())
    if raw_direct in {"A", "B"}:
        return raw_direct

    for candidate, label in speaker_aliases.items():
        if label not in {"A", "B"}:
            continue
        if _normalize_speaker_alias_key(candidate) == normalized_value:
            return label
    return None


def normalize_speaker_label(value: object, speaker_aliases: dict[str, str] | None = None) -> str:
    if not isinstance(value, str):
        return "A"

    text = value.strip()
    if not text:
        return "A"

    translated = text.translate(_SPEAKER_TRANSLATION).upper()
    if translated in {"A", "B"}:
        return translated

    translated = translated.strip(" \t\r\n:;,.!?-–—()[]{}\"'«»")

    match = re.fullmatch(r"(?:HOST|SPEAKER)?\s*([AB])", translated)
    if match:
        return match.group(1)

    alias = _lookup_speaker_alias(text, speaker_aliases)
    if alias in {"A", "B"}:
        return alias

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


def normalize_dialogue_output(
    value: Any,
    *,
    speaker_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
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
        normalized_line = {
            "speaker": normalize_speaker_label(item.get("speaker"), speaker_aliases=speaker_aliases),
            "text": cleaned_text,
            "claim_ids": normalized_claim_ids,
        }
        emotion = item.get("emotion")
        if isinstance(emotion, str) and emotion.strip():
            normalized_line["emotion"] = emotion.strip()
        ssml_hints = item.get("ssml_hints")
        if isinstance(ssml_hints, dict):
            normalized_line["ssml_hints"] = deepcopy(ssml_hints)
        dialogue.append(normalized_line)
    return dialogue or None


def merge_dialogue_metadata(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not repaired_dialogue:
        return None

    merged: list[dict[str, Any]] = []
    original_lines = [line for line in original_dialogue if isinstance(line, dict)]
    for idx, repaired_line in enumerate(repaired_dialogue):
        if not isinstance(repaired_line, dict):
            return None
        merged_line = deepcopy(repaired_line)
        original_line = original_lines[idx] if idx < len(original_lines) else None
        if not isinstance(original_line, dict):
            merged.append(merged_line)
            continue

        original_speaker = str(original_line.get("speaker") or "").strip()
        repaired_speaker = str(repaired_line.get("speaker") or "").strip()
        original_text = str(original_line.get("text") or "").strip()
        repaired_text = str(repaired_line.get("text") or "").strip()

        if original_speaker == repaired_speaker and original_text == repaired_text:
            preserved = deepcopy(original_line)
            preserved["claim_ids"] = list(repaired_line.get("claim_ids") or [])
            merged.append(preserved)
            continue

        if original_speaker == repaired_speaker and original_text != repaired_text:
            if "emotion" in original_line and "emotion" not in merged_line:
                merged_line["emotion"] = original_line.get("emotion")
            if "ssml_hints" in original_line and "ssml_hints" not in merged_line:
                merged_line["ssml_hints"] = deepcopy(original_line.get("ssml_hints"))

        merged.append(merged_line)

    return merged


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
    episode_last_key: tuple[int, int] | None = None
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
        act_last_key: tuple[int, int] | None = None
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
                "is_final_scene_in_act": False,
                "is_final_scene_in_episode": False,
            }
            act_last_key = (act_number, scene_number)
            episode_last_key = act_last_key
        if act_last_key is not None:
            lookup[act_last_key]["is_final_scene_in_act"] = True
    if episode_last_key is not None:
        lookup[episode_last_key]["is_final_scene_in_episode"] = True
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
