from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.source_content_readiness import source_version_model_eligible
from app.generation_v2.tools.evidence_refs import version_ref_for_index
from app.generation_v2.tools.evidence_tool_payloads import active_filter_list
from app.generation_v2.tools.evidence_utils import _readable_version_priority, _uuid_list, _uuid_or_none
from app.models.generation_v2 import SourceVersion


async def version_ref_for_version(owner: Any, version: SourceVersion) -> str:
    source_ref = await owner._source_ref_for_id(version.source_id)
    versions = await owner._versions_for_source(str(version.source_id))
    canonical_version_ids = set(active_filter_list(owner.run_view, "canonical_source_version_ids"))
    for idx, candidate in enumerate(versions, start=1):
        if str(candidate.id) == str(version.id):
            return version_ref_for_index(
                source_ref,
                index=idx,
                canonical=str(candidate.id) in canonical_version_ids,
            )
    return f"{source_ref}:v?"


async def source_version_id_from_ref(owner: Any, version_ref: str) -> str | None:
    text = str(version_ref or "").strip()
    if not text:
        return None
    if _uuid_or_none(text) and owner._source_version_in_run_view(text):
        return text
    if ":" not in text:
        return None
    source_ref, version_part = text.split(":", 1)
    source_id = await owner._source_id_from_ref_or_id(source_ref)
    if not source_id:
        return None
    versions = await owner._versions_for_source(source_id)
    canonical_version_ids = set(active_filter_list(owner.run_view, "canonical_source_version_ids"))
    if version_part == "canonical":
        for version in versions:
            if str(version.id) in canonical_version_ids:
                return str(version.id)
        return None
    if version_part.startswith("v") and version_part[1:].isdigit():
        idx = int(version_part[1:])
        if 1 <= idx <= len(versions):
            return str(versions[idx - 1].id)
    return None


async def invalid_source_or_version_ref_error(
    owner: Any,
    *,
    source_ref: str = "",
    version_ref: str = "",
) -> dict[str, Any] | None:
    source_ref = str(source_ref or "").strip()
    version_ref = str(version_ref or "").strip()
    if source_ref:
        resolved_source_id = await owner._source_id_from_ref_or_id(source_ref)
        if not resolved_source_id:
            return {
                "accepted": False,
                "error": "source_ref is outside this run corpus view",
                "invalid_source_ref": source_ref,
            }
        scope_error = await owner._triage_scope_error_for_source_ids([resolved_source_id])
        if scope_error:
            return scope_error
    if version_ref:
        resolved_source_version_id = await owner._source_version_id_from_ref(version_ref)
        if not resolved_source_version_id:
            return {
                "accepted": False,
                "error": "version_ref is outside this run corpus view",
                "invalid_version_ref": version_ref,
            }
        if _uuid_or_none(resolved_source_version_id):
            version = await owner.db.get(SourceVersion, uuid.UUID(resolved_source_version_id))
            if version is not None:
                scope_error = await owner._triage_scope_error_for_source_ids([str(version.source_id)])
                if scope_error:
                    return scope_error
    return None


async def versions_for_source(owner: Any, source_id: str) -> list[SourceVersion]:
    result = await owner.db.execute(
        select(SourceVersion).where(
            SourceVersion.source_id == uuid.UUID(source_id),
            SourceVersion.id.in_(_uuid_list([str(item) for item in owner.run_view.selected_source_version_ids])),
        )
    )
    return sorted(
        [version for version in result.scalars() if source_version_model_eligible(version)],
        key=lambda version: _readable_version_priority(version.kind),
    )


__all__ = [
    "invalid_source_or_version_ref_error",
    "source_version_id_from_ref",
    "version_ref_for_version",
    "versions_for_source",
]
