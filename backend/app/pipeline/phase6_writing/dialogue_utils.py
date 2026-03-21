"""Shared dialogue helpers for phase 6 writing/audit passes."""

from __future__ import annotations

import re
from typing import Any

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
