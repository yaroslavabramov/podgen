from __future__ import annotations

import json
from typing import Any

from app.models.generation_v2 import EvidenceLocatorType, EvidenceStrength, SourceRepresentationKind, SourceVersion


MODEL_FILE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "supports_query": {"type": "boolean"},
        "support_level": {"type": "string", "enum": ["direct", "supported", "weak", "unsupported", "not_found"]},
        "quote_text": {"type": "string"},
        "snippet_text": {"type": "string"},
        "support_summary": {"type": "string"},
        "uncertainty": {"type": "string"},
        "locator_text": {"type": "string"},
        "locator_type": {"type": "string", "enum": ["page", "section", "timestamp", "provider_snippet", "source_level", "unknown"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "supports_query",
        "support_level",
        "quote_text",
        "snippet_text",
        "support_summary",
        "uncertainty",
        "locator_text",
        "locator_type",
        "confidence",
    ],
}


def model_file_read_prompt(
    *,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
    source_id: str,
    source_version_id: str,
    source_kind: str,
) -> str:
    return (
        "Read the attached file as source evidence for a long-form podcast episode.\n"
        f"source_id: {source_id}\n"
        f"source_version_id: {source_version_id}\n"
        f"source_kind: {source_kind}\n"
        f"purpose: {purpose}\n"
        f"focus_question: {focus_question}\n"
        f"claim_text: {claim_text}\n"
        f"locator_hint: {locator_hint}\n\n"
        "Task:\n"
        "- Use only the attached file.\n"
        "- Find the strongest relevant support for the focus question or claim.\n"
        "- Extract a direct quote when one is available; otherwise provide a concise snippet-like paraphrase grounded in the file.\n"
        "- Provide support_summary that explains what the file supports and any uncertainty.\n"
        "- Include locator_text only if the file provides a useful page, section, heading, timestamp, or filename hint. Page numbers are helpful but optional.\n"
        "- If the file does not support the claim/question, set supports_query=false and support_level=unsupported or not_found.\n"
    )


def parse_model_file_read_result(output_text: str) -> dict[str, Any]:
    try:
        data = json.loads(output_text or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model file read returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Model file read returned a non-object JSON payload")
    normalized = {
        "supports_query": bool(data.get("supports_query")),
        "support_level": str(data.get("support_level") or "unsupported"),
        "quote_text": str(data.get("quote_text") or ""),
        "snippet_text": str(data.get("snippet_text") or ""),
        "support_summary": str(data.get("support_summary") or ""),
        "uncertainty": str(data.get("uncertainty") or ""),
        "locator_text": str(data.get("locator_text") or ""),
        "locator_type": str(data.get("locator_type") or EvidenceLocatorType.UNKNOWN),
        "confidence": confidence(data.get("confidence")),
    }
    if normalized["support_level"] not in {"direct", "supported", "weak", "unsupported", "not_found"}:
        normalized["support_level"] = "unsupported"
    if normalized["locator_type"] not in _MODEL_READ_LOCATOR_TYPES:
        normalized["locator_type"] = EvidenceLocatorType.UNKNOWN
    return normalized


def model_file_read_snippet(read_result: dict[str, Any]) -> str:
    parts = []
    quote = str(read_result.get("quote_text") or "").strip()
    snippet = str(read_result.get("snippet_text") or "").strip()
    summary = str(read_result.get("support_summary") or "").strip()
    uncertainty = str(read_result.get("uncertainty") or "").strip()
    if quote:
        parts.append(f"Quote: {quote}")
    if snippet and snippet != quote:
        parts.append(f"Snippet: {snippet}")
    if summary:
        parts.append(f"Support summary: {summary}")
    if uncertainty:
        parts.append(f"Uncertainty: {uncertainty}")
    return "\n".join(parts)


def model_read_evidence_strength(read_result: dict[str, Any]) -> str:
    support_level = str(read_result.get("support_level") or "")
    if support_level == "direct" and str(read_result.get("quote_text") or "").strip():
        return EvidenceStrength.EXACT
    if support_level in {"direct", "supported"}:
        return EvidenceStrength.SNIPPET
    return EvidenceStrength.WEAK


def model_read_locator_type(read_result: dict[str, Any]) -> str:
    locator_type = str(read_result.get("locator_type") or EvidenceLocatorType.UNKNOWN)
    if locator_type in _MODEL_READ_LOCATOR_TYPES:
        return locator_type
    return EvidenceLocatorType.UNKNOWN


def model_read_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.ORIGINAL_FILE: 0,
        SourceRepresentationKind.PAGE_MARKED_TEXT: 1,
        SourceRepresentationKind.EXTRACTED_TEXT: 2,
        SourceRepresentationKind.TRANSCRIPT: 2,
        SourceRepresentationKind.RENDERED_SNAPSHOT: 3,
        SourceRepresentationKind.FETCHED_HTML: 4,
    }
    return order.get(kind, 100)


def model_read_upload_payload(version: SourceVersion) -> tuple[str, bytes]:
    from app.generation_v2.source_corpus import _version_upload_payload

    return _version_upload_payload(version)


def confidence(score: Any) -> float:
    if score is None:
        return 0.7
    try:
        return max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return 0.7


_MODEL_READ_LOCATOR_TYPES = {
    EvidenceLocatorType.PAGE,
    EvidenceLocatorType.SECTION,
    EvidenceLocatorType.TIMESTAMP,
    EvidenceLocatorType.PROVIDER_SNIPPET,
    EvidenceLocatorType.SOURCE_LEVEL,
    EvidenceLocatorType.UNKNOWN,
}


__all__ = [
    "MODEL_FILE_READ_SCHEMA",
    "confidence",
    "model_file_read_prompt",
    "model_file_read_snippet",
    "model_read_evidence_strength",
    "model_read_locator_type",
    "model_read_upload_payload",
    "model_read_version_priority",
    "parse_model_file_read_result",
]
