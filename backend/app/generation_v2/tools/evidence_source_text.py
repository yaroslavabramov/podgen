from __future__ import annotations

import hashlib

from app.models.generation_v2 import SourceRepresentationKind, SourceVersion
from app.services.storage import download_text, get_minio_client


def source_version_text(version: SourceVersion) -> str:
    if not version.storage_path or version.kind == SourceRepresentationKind.ORIGINAL_URL:
        return ""
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE and not looks_text_readable_path(version.storage_path):
        return ""
    try:
        text = download_text(get_minio_client(), version.storage_path)
    except Exception:
        return ""
    expected_hash = str(version.content_hash or "").strip().lower()
    if expected_hash and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_hash:
        return ""
    return text


def looks_text_readable_path(path: str) -> bool:
    lowered = path.lower().split("?", 1)[0]
    return lowered.endswith((".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".tsv", ".xml", ".srt", ".vtt"))


__all__ = ["looks_text_readable_path", "source_version_text"]
