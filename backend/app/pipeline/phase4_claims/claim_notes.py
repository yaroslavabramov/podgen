"""Internal metadata helpers for Claim.notes."""

from __future__ import annotations

import json

_CLAIM_META_PREFIX = "__claim_meta__:"


def encode_claim_notes_metadata(*, branch_id: str, question_ids: list[str]) -> str:
    payload = {
        "builder_branch_id": branch_id,
        "builder_question_ids": sorted({qid for qid in question_ids if isinstance(qid, str) and qid.strip()}),
    }
    return _CLAIM_META_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def decode_claim_notes_metadata(notes: str | None) -> dict:
    if not isinstance(notes, str) or not notes.startswith(_CLAIM_META_PREFIX):
        return {}
    raw = notes[len(_CLAIM_META_PREFIX):]
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_editor_claim_notes(notes: str | None) -> str:
    if not isinstance(notes, str):
        return ""
    if notes.startswith(_CLAIM_META_PREFIX):
        return ""
    return notes
