from __future__ import annotations

from app.generation_v2.source_corpus_helpers import provider_filename
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion
from app.services.storage import download_file, download_text, get_minio_client


def version_upload_payload(version: SourceVersion) -> tuple[str, bytes]:
    if not version.storage_path:
        return provider_filename(version, ".txt"), b""
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        return provider_filename(version), download_file(get_minio_client(), version.storage_path)
    text = download_text(get_minio_client(), version.storage_path)
    return provider_filename(version, ".txt"), text.encode("utf-8")


__all__ = ["version_upload_payload"]
