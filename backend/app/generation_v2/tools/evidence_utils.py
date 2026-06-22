from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import urlparse

from app.models.chunk import Chunk
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)
from app.models.source import Source
from app.services.storage import download_text, get_minio_client


def _provider_filters(source_ids: list[str]) -> dict[str, Any]:
    valid = [source_id for source_id in source_ids if _uuid_or_none(source_id)]
    base_filters = [
        {"type": "eq", "key": "is_active", "value": True},
        {"type": "eq", "key": "agent_facing", "value": True},
        {"type": "eq", "key": "canonical_retrieval_surface", "value": True},
    ]
    if not valid:
        return {"type": "and", "filters": base_filters}
    source_filters = [{"type": "eq", "key": "source_id", "value": source_id} for source_id in valid]
    return {
        "type": "and",
        "filters": [
            *base_filters,
            {"type": "or", "filters": source_filters} if len(source_filters) > 1 else source_filters[0],
        ],
    }


def _provider_snippet(item: Any) -> str:
    content = getattr(item, "content", []) or []
    return "\n\n".join(str(_part_text(part) or "") for part in content if _part_text(part)).strip()


def _part_text(part: Any) -> str | None:
    if isinstance(part, dict):
        return part.get("text")
    return getattr(part, "text", None)


def _source_version_text(version: SourceVersion) -> str:
    if not version.storage_path or version.kind == SourceRepresentationKind.ORIGINAL_URL:
        return ""
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE and not _looks_text_readable_path(version.storage_path):
        return ""
    try:
        return download_text(get_minio_client(), version.storage_path)
    except Exception:
        return ""


def _looks_text_readable_path(path: str) -> bool:
    lowered = path.lower().split("?", 1)[0]
    return lowered.endswith((".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".tsv", ".xml", ".srt", ".vtt"))


_MODEL_FILE_READ_SCHEMA: dict[str, Any] = {
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


def _model_file_read_prompt(
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


def _parse_model_file_read_result(output_text: str) -> dict[str, Any]:
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
        "confidence": _confidence(data.get("confidence")),
    }
    if normalized["support_level"] not in {"direct", "supported", "weak", "unsupported", "not_found"}:
        normalized["support_level"] = "unsupported"
    if normalized["locator_type"] not in {
        EvidenceLocatorType.PAGE,
        EvidenceLocatorType.SECTION,
        EvidenceLocatorType.TIMESTAMP,
        EvidenceLocatorType.PROVIDER_SNIPPET,
        EvidenceLocatorType.SOURCE_LEVEL,
        EvidenceLocatorType.UNKNOWN,
    }:
        normalized["locator_type"] = EvidenceLocatorType.UNKNOWN
    return normalized


def _model_file_read_snippet(read_result: dict[str, Any]) -> str:
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


def _model_read_evidence_strength(read_result: dict[str, Any]) -> str:
    support_level = str(read_result.get("support_level") or "")
    if support_level == "direct" and str(read_result.get("quote_text") or "").strip():
        return EvidenceStrength.EXACT
    if support_level in {"direct", "supported"}:
        return EvidenceStrength.SNIPPET
    if support_level == "weak":
        return EvidenceStrength.WEAK
    return EvidenceStrength.WEAK


def _model_read_locator_type(read_result: dict[str, Any]) -> str:
    locator_type = str(read_result.get("locator_type") or EvidenceLocatorType.UNKNOWN)
    if locator_type in {
        EvidenceLocatorType.PAGE,
        EvidenceLocatorType.SECTION,
        EvidenceLocatorType.TIMESTAMP,
        EvidenceLocatorType.PROVIDER_SNIPPET,
        EvidenceLocatorType.SOURCE_LEVEL,
        EvidenceLocatorType.UNKNOWN,
    }:
        return locator_type
    return EvidenceLocatorType.UNKNOWN


def _model_read_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.ORIGINAL_FILE: 0,
        SourceRepresentationKind.PAGE_MARKED_TEXT: 1,
        SourceRepresentationKind.EXTRACTED_TEXT: 2,
        SourceRepresentationKind.TRANSCRIPT: 2,
        SourceRepresentationKind.RENDERED_SNAPSHOT: 3,
        SourceRepresentationKind.FETCHED_HTML: 4,
    }
    return order.get(kind, 100)


def _model_read_upload_payload(version: SourceVersion) -> tuple[str, bytes]:
    from app.generation_v2.source_corpus import _version_upload_payload

    return _version_upload_payload(version)


def _unsupported_model_read_selected_as_support(item: EvidenceItem, *, purpose: str, role: str) -> bool:
    if item.retrieval_channel != EvidenceRetrievalChannel.MODEL_FILE_READ:
        return False
    metadata = item.metadata_ or {}
    support_level = str(metadata.get("support_level") or "")
    if bool(metadata.get("supports_query", True)) and support_level not in {"weak", "unsupported", "not_found"}:
        return False
    allowed = {
        "gap",
        "gaps",
        "caution",
        "uncertainty",
        "contradiction",
        "negative_finding",
        "no_support",
        "not_found",
    }
    purpose_role = {part.lower().strip() for part in [purpose, role] if part}
    return not bool(purpose_role.intersection(allowed))


def _quote_text_matches_evidence(quote: str, snippet: str) -> bool:
    normalized_quote = _normalize(quote)
    normalized_snippet = _normalize(snippet)
    if not normalized_quote or not normalized_snippet:
        return False
    if normalized_quote in normalized_snippet:
        return True
    quote_tokens = set(_query_tokens(normalized_quote))
    if not quote_tokens:
        return False
    overlap = quote_tokens.intersection(_query_tokens(normalized_snippet))
    return len(overlap) / max(1, len(quote_tokens)) >= 0.75


def _query_tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[^\W_]{3,}", (text or "").lower(), flags=re.UNICODE) if token]



def _hosted_result_file_id(result: dict[str, Any]) -> str | None:
    for key in ("file_id", "fileId", "id"):
        value = result.get(key)
        if value:
            return str(value)
    return None


def _hosted_result_text(result: dict[str, Any]) -> str:
    text = result.get("text")
    if isinstance(text, str):
        return text
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        return "\n\n".join(part for part in parts if part).strip()
    return str(result.get("snippet") or result.get("quote") or "")


def _safe_metadata(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    for key in ("file_id", "filename", "score", "attributes", "result_index"):
        if key in result:
            allowed[key] = result[key]
    return allowed


def _uuid_list(values: list[str]) -> list[uuid.UUID]:
    return [uuid.UUID(str(value)) for value in values if _uuid_or_none(str(value))]


def _readable_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.PAGE_MARKED_TEXT: 0,
        SourceRepresentationKind.EXTRACTED_TEXT: 1,
        SourceRepresentationKind.TRANSCRIPT: 1,
        SourceRepresentationKind.RENDERED_SNAPSHOT: 2,
        SourceRepresentationKind.FETCHED_HTML: 3,
        SourceRepresentationKind.ORIGINAL_FILE: 10,
        SourceRepresentationKind.ORIGINAL_URL: 100,
    }
    return order.get(kind, 50)


def _retrieval_channel_for_version(version: SourceVersion) -> str:
    if version.kind == SourceRepresentationKind.TRANSCRIPT:
        return EvidenceRetrievalChannel.LOCAL_TRANSCRIPT_SEARCH
    if version.kind in {SourceRepresentationKind.FETCHED_HTML, SourceRepresentationKind.RENDERED_SNAPSHOT}:
        return EvidenceRetrievalChannel.WEB_SNAPSHOT
    return EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH


def _window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str]:
    normalized = text.strip()
    if not normalized:
        return "", locator or "source window", EvidenceLocatorType.UNKNOWN
    page_window = _page_window(normalized, locator=locator, max_chars=max_chars)
    if page_window:
        return page_window
    timestamp_window = _timestamp_window(normalized, locator=locator, max_chars=max_chars)
    if timestamp_window:
        return timestamp_window
    heading_window = _heading_window(normalized, locator=locator, max_chars=max_chars)
    if heading_window:
        return heading_window
    if locator:
        idx = normalized.lower().find(locator.lower())
        if idx >= 0:
            start = max(0, idx - max_chars // 4)
            end = min(len(normalized), start + max_chars)
            return normalized[start:end], f"around phrase: {locator}", EvidenceLocatorType.SECTION
    return normalized[:max_chars], locator or "source beginning", EvidenceLocatorType.SOURCE_LEVEL


def _source_outline(text: str, *, max_items: int) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    items: list[dict[str, Any]] = []
    for page in _page_markers(text)[:max_items]:
        items.append({"locator": f"page {page['page']}", "locator_type": EvidenceLocatorType.PAGE, "preview": page["line"][:240]})
    if len(items) < max_items:
        for timestamp in _timestamp_markers(text)[: max_items - len(items)]:
            items.append(
                {
                    "locator": timestamp["timestamp"],
                    "locator_type": EvidenceLocatorType.TIMESTAMP,
                    "preview": timestamp["line"][:240],
                }
            )
    if len(items) < max_items:
        for heading in _heading_markers(text)[: max_items - len(items)]:
            items.append({"locator": heading["heading"], "locator_type": EvidenceLocatorType.SECTION, "preview": heading["line"][:240]})
    return items


def _page_window(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    page_range = _parse_page_locator(locator)
    if not page_range:
        return None
    start_page, end_page = page_range
    markers = _page_markers(text)
    if not markers:
        return None
    start_marker = next((marker for marker in markers if marker["page"] == start_page), None)
    if start_marker is None:
        return None
    end_marker = next((marker for marker in markers if marker["page"] > end_page), None)
    start = int(start_marker["start"])
    end = int(end_marker["start"]) if end_marker else len(text)
    snippet = text[start:end].strip()[:max_chars]
    label = f"page {start_page}" if start_page == end_page else f"pages {start_page}-{end_page}"
    return snippet, label, EvidenceLocatorType.PAGE


def _timestamp_window(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    target_seconds = _parse_timestamp(locator)
    if target_seconds is None:
        return None
    markers = _timestamp_markers(text)
    if not markers:
        return None
    nearest = min(markers, key=lambda marker: abs(int(marker["seconds"]) - target_seconds))
    start = max(0, int(nearest["start"]) - max_chars // 3)
    end = min(len(text), start + max_chars)
    return text[start:end].strip(), str(nearest["timestamp"]), EvidenceLocatorType.TIMESTAMP


def _heading_window(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    if not locator.strip():
        return None
    lower = locator.lower()
    for heading in _heading_markers(text):
        if lower in str(heading["heading"]).lower():
            start = int(heading["start"])
            end = min(len(text), start + max_chars)
            return text[start:end].strip(), str(heading["heading"]), EvidenceLocatorType.SECTION
    return None


def _search_text_windows(text: str, *, query: str, max_windows: int, max_chars: int) -> list[dict[str, Any]]:
    tokens = _query_tokens(query)
    if not tokens:
        snippet = text.strip()[:max_chars]
        return [{"snippet_text": snippet, "locator_text": "source beginning", "locator_type": EvidenceLocatorType.SOURCE_LEVEL, "score": 0.0}]
    matches: list[dict[str, Any]] = []
    lower = text.lower()
    seen_starts: set[int] = set()
    for token in tokens[:8]:
        for match in re.finditer(re.escape(token), lower):
            start = max(0, match.start() - max_chars // 4)
            start = max(marker for marker in [0, start] if marker >= 0)
            if any(abs(start - seen) < max_chars // 3 for seen in seen_starts):
                continue
            seen_starts.add(start)
            end = min(len(text), start + max_chars)
            snippet = text[start:end].strip()
            locator = _nearest_locator_for_position(text, match.start()) or f"around phrase: {token}"
            score = sum(snippet.lower().count(item) for item in tokens[:8])
            matches.append({"snippet_text": snippet, "locator_text": locator, "locator_type": _locator_type_for_label(locator), "score": float(score)})
            if len(matches) >= max_windows:
                return sorted(matches, key=lambda item: item["score"], reverse=True)
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:max_windows]


def _page_markers(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?im)^\s*(?:-{2,}\s*)?(?:\[?\s*(?:page|p\.)\s+(\d+)\s*\]?)(?:\s*-{2,})?\s*$")
    return [
        {
            "page": int(match.group(1)),
            "start": match.start(),
            "line": match.group(0).strip(),
        }
        for match in pattern.finditer(text)
    ]


def _heading_markers(text: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        is_heading = bool(re.match(r"^#{1,6}\s+\S", stripped)) or (
            4 <= len(stripped) <= 120
            and not stripped.endswith(".")
            and stripped[:1].isupper()
            and len(stripped.split()) <= 14
        )
        if is_heading:
            markers.append({"heading": stripped.lstrip("# ").strip(), "start": offset, "line": stripped})
        offset += len(line)
    return markers


def _timestamp_markers(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?m)(?:^|\s|\[)(\d{1,2}:\d{2}(?::\d{2})?)(?:\]|\s|$)")
    markers = []
    for match in pattern.finditer(text):
        seconds = _parse_timestamp(match.group(1))
        if seconds is None:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        markers.append({"timestamp": match.group(1), "seconds": seconds, "start": match.start(), "line": text[line_start:line_end].strip()})
    return markers


def _parse_page_locator(locator: str) -> tuple[int, int] | None:
    match = re.search(r"\bpages?\s+(\d+)(?:\s*[-–]\s*(\d+))?\b|\bp\.\s*(\d+)\b", locator or "", flags=re.IGNORECASE)
    if not match:
        return None
    start = int(match.group(1) or match.group(3))
    end = int(match.group(2) or start)
    return min(start, end), max(start, end)


def _parse_timestamp(value: str) -> int | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", value or "")
    if not match:
        return None
    first, second, third = match.groups()
    if third is None:
        return int(first) * 60 + int(second)
    return int(first) * 3600 + int(second) * 60 + int(third)


def _nearest_locator_for_position(text: str, position: int) -> str | None:
    candidates: list[tuple[int, str]] = []
    for marker in _page_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), f"page {marker['page']}"))
    for marker in _timestamp_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["timestamp"])))
    for marker in _heading_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["heading"])))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _locator_type_for_label(label: str) -> str:
    if _parse_page_locator(label):
        return EvidenceLocatorType.PAGE
    if _parse_timestamp(label) is not None:
        return EvidenceLocatorType.TIMESTAMP
    if label.startswith("around phrase:"):
        return EvidenceLocatorType.SECTION
    return EvidenceLocatorType.SECTION


def _select_chunk_window(chunks: list[Chunk], *, locator: str) -> Chunk:
    if locator:
        lower = locator.lower()
        for chunk in chunks:
            heading = " > ".join(chunk.heading_path or [])
            if lower in heading.lower() or lower in chunk.content.lower():
                return chunk
    return chunks[0]


def _chunk_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.EXTRACTED_TEXT: 0,
        SourceRepresentationKind.TRANSCRIPT: 0,
    }
    return order.get(kind, 100)


def _source_domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or None


def _source_length_hint(source: Source, versions: list[SourceVersion]) -> str:
    metadata = source.metadata_ or {}
    for key in ("char_count", "token_count", "page_count", "duration_seconds"):
        if metadata.get(key) is not None:
            return f"{key}={metadata[key]}"
    if source.text_preview:
        return f"preview_chars={len(source.text_preview)}"
    readable = [version for version in versions if _version_readable(version)]
    return f"readable_versions={len(readable)}"


def _provider_status(statuses: list[str]) -> str:
    if not statuses:
        return "not_uploaded"
    if any(status == V2RecordStatus.READY for status in statuses):
        return "ready"
    if any(status == V2RecordStatus.PENDING for status in statuses):
        return "pending"
    if all(status == V2RecordStatus.FAILED for status in statuses):
        return "failed"
    return "mixed"


def _neutral_preview(source: Source, versions: list[SourceVersion]) -> str:
    preview = (source.text_preview or "").strip()
    if preview:
        return preview[:700]
    version_labels = ", ".join(f"{version.kind}:{version.status}" for version in versions[:6])
    return f"Neutral source metadata only. Available versions: {version_labels or 'none'}."


def _inventory_cautions(source: Source, versions: list[SourceVersion], provider_statuses: list[str]) -> list[str]:
    cautions: list[str] = []
    if source.source_type == "web" and not source.published_at:
        cautions.append("Publication date is missing.")
    if not any(_version_readable(version) for version in versions):
        cautions.append("No local readable snapshot/text version is available; provider search may be required.")
    if provider_statuses and not any(status == V2RecordStatus.READY for status in provider_statuses):
        cautions.append("Provider index is not ready for this source.")
    return cautions


def _version_readable(version: SourceVersion) -> bool:
    return bool(version.status == V2RecordStatus.READY and version.kind != SourceRepresentationKind.ORIGINAL_URL)


def _version_can_back_local_window(version: SourceVersion) -> bool:
    if version.status != V2RecordStatus.READY or not version.storage_path:
        return False
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        return _looks_text_readable_path(version.storage_path)
    return version.kind in {
        SourceRepresentationKind.PAGE_MARKED_TEXT,
        SourceRepresentationKind.EXTRACTED_TEXT,
        SourceRepresentationKind.TRANSCRIPT,
        SourceRepresentationKind.RENDERED_SNAPSHOT,
        SourceRepresentationKind.FETCHED_HTML,
    }


def _confidence(score: Any) -> float:
    if score is None:
        return 0.7
    try:
        return max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return 0.7


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()

__all__ = [name for name in globals() if not name.startswith('__')]
