from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.generation_v2.source_corpus_contracts import (
    PROVIDER_INTERNAL_ATTRIBUTE_KEYS,
    PROVIDER_READY_PHASES,
    ProviderUploadPhase,
)
from app.generation_v2.source_content_readiness import (
    source_version_content_state,
    source_version_model_eligible,
)
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, SourceRepresentationKind, SourceVersion, V2RecordStatus


def provider_file_search_ready(provider_file: ProviderCorpusFile) -> bool:
    attributes = provider_file.attributes or {}
    return bool(
        provider_file.provider_file_id
        and provider_file.status == V2RecordStatus.READY
        and attributes.get("is_active", True) is True
        and attributes.get("agent_facing", True) is True
        and attributes.get("canonical_retrieval_surface", True) is True
        and attributes.get("content_confirmed", True) is True
    )


def provider_filename(version: SourceVersion, suffix: str | None = None) -> str:
    metadata = version.metadata_ or {}
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        raw_name = str(metadata.get("original_filename") or f"{version.source_id}-{version.kind}")
        filename = Path(raw_name).name or f"{version.source_id}-{version.kind}"
        if metadata.get("source_type") == "pdf" and Path(filename).suffix.lower() != ".pdf":
            filename = f"{filename}.pdf"
    else:
        filename = f"{version.source_id}-{version.kind}-{version.id}{suffix or ''}"
    return filename.replace("/", "_")


def validate_reconciliation_candidate(
    provider_index: ProviderCorpusIndex,
    vector_file: Any,
    attributes: dict[str, Any],
    versions_by_id: dict[uuid.UUID, SourceVersion],
    canonical_by_source: dict[uuid.UUID, SourceVersion],
    scope_source_ids: set[uuid.UUID],
) -> dict[str, Any]:
    provider_file_id = str(getattr(vector_file, "id", "") or "")
    if not provider_file_id:
        return {"valid": False, "reason": "missing_provider_file_id"}
    project_id = str(attributes.get("project_id") or "")
    if project_id and project_id != str(provider_index.project_id):
        return {"valid": False, "reason": "wrong_project"}
    source_id = parse_uuid(attributes.get("source_id"))
    if source_id is None:
        if len(scope_source_ids) <= 1:
            return {"valid": False, "ignored": True, "reason": "source_outside_sync_scope"}
        return {"valid": False, "reason": "missing_source_id"}
    if source_id not in scope_source_ids:
        return {"valid": False, "ignored": True, "reason": "source_outside_sync_scope"}
    version_id = parse_uuid(attributes.get("source_version_id"))
    if version_id is None:
        return {"valid": False, "reason": "missing_source_version_id"}
    version = versions_by_id.get(version_id)
    if version is None:
        return {"valid": False, "reason": "missing_source_version"}
    if version.source_id != source_id:
        return {"valid": False, "reason": "source_version_source_mismatch"}
    if not version.is_active or version.status != V2RecordStatus.READY or not version_uploadable(version):
        return {"valid": False, "reason": "source_version_not_active_ready"}
    canonical = canonical_by_source.get(version.source_id)
    if canonical is None or canonical.id != version.id:
        return {"valid": False, "reason": "noncanonical_source_version"}
    representation_kind = str(attributes.get("representation_kind") or "")
    if representation_kind and representation_kind != str(version.kind):
        return {"valid": False, "reason": "representation_kind_mismatch"}
    content_hash = str(attributes.get("content_hash") or "")
    if content_hash and version.content_hash and content_hash != version.content_hash:
        return {"valid": False, "reason": "content_hash_mismatch"}
    return {"valid": True, "version": version}


def parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def provider_db_attributes(
    current: dict[str, Any] | None,
    phase: str | ProviderUploadPhase,
    provider_attributes: dict[str, str | bool | float],
    **extra: Any,
) -> dict[str, Any]:
    merged = dict(current or {})
    if phase in PROVIDER_READY_PHASES:
        for stale_key in ("active_index_status", "excluded_reason", "last_openai_error"):
            merged.pop(stale_key, None)
    return {
        **merged,
        **provider_attributes,
        "upload_phase": phase,
        **{key: value for key, value in extra.items() if value is not None},
    }


def provider_external_attributes(attributes: dict[str, Any]) -> dict[str, str | bool | float]:
    return {
        key: value
        for key, value in attributes.items()
        if key not in PROVIDER_INTERNAL_ATTRIBUTE_KEYS and isinstance(value, (str, bool, float, int))
    }


def provider_last_error(vector_file: Any) -> str | None:
    last_error = getattr(vector_file, "last_error", None)
    if isinstance(last_error, dict):
        return str(last_error.get("message") or last_error.get("code") or "") or None
    message = getattr(last_error, "message", None)
    code = getattr(last_error, "code", None)
    if message or code:
        return str(message or code)
    return None


def provider_attributes(version: SourceVersion) -> dict[str, str | bool | float]:
    metadata = version.metadata_ or {}
    return {
        "project_id": str(version.project_id),
        "source_id": str(version.source_id),
        "source_version_id": str(version.id),
        "source_type": str(metadata.get("source_type") or "unknown"),
        "representation_kind": version.kind,
        "content_hash": version.content_hash or "",
        "content_state": source_version_content_state(version),
        "content_confirmed": source_version_model_eligible(version),
        "authority": str(metadata.get("authority") or "unknown"),
        "is_active": bool(version.is_active),
        "agent_facing": True,
        "canonical_retrieval_surface": True,
        "retrieval_surface": "canonical",
        "scope": "project",
    }


def version_uploadable(version: SourceVersion) -> bool:
    return source_version_model_eligible(version)


__all__ = [
    "parse_uuid",
    "provider_attributes",
    "provider_db_attributes",
    "provider_external_attributes",
    "provider_file_search_ready",
    "provider_filename",
    "provider_last_error",
    "validate_reconciliation_candidate",
    "version_uploadable",
]
