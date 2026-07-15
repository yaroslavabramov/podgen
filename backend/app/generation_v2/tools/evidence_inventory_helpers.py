from __future__ import annotations

from urllib.parse import urlparse

from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source


def readable_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.EXTRACTED_TEXT: 0,
        SourceRepresentationKind.TRANSCRIPT: 0,
        SourceRepresentationKind.PAGE_MARKED_TEXT: 2,
        SourceRepresentationKind.RENDERED_SNAPSHOT: 2,
        SourceRepresentationKind.FETCHED_HTML: 3,
        SourceRepresentationKind.ORIGINAL_FILE: 10,
        SourceRepresentationKind.ORIGINAL_URL: 100,
    }
    return order.get(kind, 50)


def source_domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or None


def source_length_hint(source: Source, versions: list[SourceVersion]) -> str:
    metadata = source.metadata_ or {}
    for key in ("char_count", "token_count", "page_count", "duration_seconds"):
        if metadata.get(key) is not None:
            return f"{key}={metadata[key]}"
    if source.text_preview:
        return f"preview_chars={len(source.text_preview)}"
    readable = [version for version in versions if version_readable(version)]
    return f"readable_versions={len(readable)}"


def provider_status(statuses: list[str]) -> str:
    if not statuses:
        return "not_uploaded"
    if any(status == V2RecordStatus.READY for status in statuses):
        return "ready"
    if any(status == V2RecordStatus.PENDING for status in statuses):
        return "pending"
    if all(status == V2RecordStatus.FAILED for status in statuses):
        return "failed"
    return "mixed"


def neutral_preview(source: Source, versions: list[SourceVersion]) -> str:
    preview = (source.text_preview or "").strip()
    if preview:
        return preview
    version_labels = ", ".join(f"{version.kind}:{version.status}" for version in versions[:6])
    return f"Neutral source metadata only. Available versions: {version_labels or 'none'}."


def inventory_cautions(source: Source, versions: list[SourceVersion], provider_statuses: list[str]) -> list[str]:
    cautions: list[str] = []
    if source.source_type == "web" and not source.published_at:
        cautions.append("Publication date is missing.")
    if not any(version_readable(version) for version in versions):
        cautions.append("No local readable snapshot/text version is available; provider search may be required.")
    if provider_statuses and not any(status == V2RecordStatus.READY for status in provider_statuses):
        cautions.append("Provider index is not ready for this source.")
    return cautions


def version_readable(version: SourceVersion) -> bool:
    return bool(version.status == V2RecordStatus.READY and version.kind != SourceRepresentationKind.ORIGINAL_URL)


__all__ = [
    "inventory_cautions",
    "neutral_preview",
    "provider_status",
    "readable_version_priority",
    "source_domain",
    "source_length_hint",
    "version_readable",
]
