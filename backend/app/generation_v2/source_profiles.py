from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas_source import SourceProfile
from app.models.generation_v2 import (
    ProjectSourceProfile,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)


SOURCE_PROFILE_SCHEMA_VERSION = "source_profile.v2"


def source_profile_fingerprint(source_version: SourceVersion) -> str:
    identity = {
        "source_version_id": str(source_version.id),
        "source_id": str(source_version.source_id),
        "content_hash": str(source_version.content_hash or ""),
        "kind": str(source_version.kind or ""),
        "storage_path": str(source_version.storage_path or ""),
        "provider_file_id": str(source_version.provider_file_id or ""),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def legacy_source_profile(source_version: SourceVersion) -> SourceProfile:
    metadata = dict(source_version.metadata_ or {})
    neutral_summary = str(metadata.get("neutral_summary") or metadata.get("summary") or "")
    return SourceProfile(
        schema_version=SOURCE_PROFILE_SCHEMA_VERSION,
        project_id=str(source_version.project_id),
        source_id=str(source_version.source_id),
        source_version_id=str(source_version.id),
        neutral_summary=neutral_summary,
        outline_hints=[str(item) for item in metadata.get("outline_hints") or []],
        source_kind=semantic_source_kind(metadata.get("source_type")),
        publisher=str(metadata.get("publisher") or ""),
        language=str(metadata.get("language") or ""),
        published_at=str(metadata.get("published_at")) if metadata.get("published_at") else None,
        representation_preference=str(metadata.get("representation_preference") or source_version.kind or ""),
        readability_warnings=[str(item) for item in metadata.get("readability_warnings") or []],
        stable_cautions=[str(item) for item in metadata.get("stable_cautions") or []],
        topic_tags=[str(item) for item in metadata.get("topic_tags") or []],
        duplicate_fingerprint=str(metadata.get("duplicate_fingerprint") or ""),
        profile_origin="legacy_adapter",
    )


async def load_current_source_profile(
    db: AsyncSession,
    source_version_id: uuid.UUID | str,
    *,
    schema_version: str = SOURCE_PROFILE_SCHEMA_VERSION,
) -> ProjectSourceProfile | None:
    result = await db.execute(
        select(ProjectSourceProfile).where(
            ProjectSourceProfile.source_version_id == uuid.UUID(str(source_version_id)),
            ProjectSourceProfile.schema_version == schema_version,
            ProjectSourceProfile.is_current.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def resolve_source_profile(db: AsyncSession, source_version: SourceVersion) -> SourceProfile:
    persisted = await load_current_source_profile(db, source_version.id)
    if persisted is not None:
        return SourceProfile.model_validate(persisted.payload)
    return legacy_source_profile(source_version)


async def save_source_profile(
    db: AsyncSession,
    *,
    source_version: SourceVersion,
    profile: SourceProfile | dict[str, Any],
    status: str = V2RecordStatus.READY,
    provider: str | None = None,
    model: str | None = None,
    prompt_hash: str | None = None,
) -> ProjectSourceProfile:
    parsed = profile if isinstance(profile, SourceProfile) else SourceProfile.model_validate(profile)
    _validate_profile_identity(parsed, source_version)
    payload = parsed.model_dump(mode="json")
    fingerprint = source_profile_fingerprint(source_version)
    # Lock the immutable source-version row so two runs cannot both create a
    # current profile for a version that did not have one yet.
    await db.execute(select(SourceVersion.id).where(SourceVersion.id == source_version.id).with_for_update())
    current_result = await db.execute(
        select(ProjectSourceProfile)
        .where(
            ProjectSourceProfile.source_version_id == source_version.id,
            ProjectSourceProfile.schema_version == parsed.schema_version,
            ProjectSourceProfile.is_current.is_(True),
        )
        .with_for_update()
    )
    current = current_result.scalar_one_or_none()
    if current is not None and current.source_fingerprint == fingerprint and current.payload == payload:
        return current

    version_result = await db.execute(
        select(func.max(ProjectSourceProfile.version)).where(
            ProjectSourceProfile.source_version_id == source_version.id,
            ProjectSourceProfile.schema_version == parsed.schema_version,
        )
    )
    next_version = int(version_result.scalar() or 0) + 1
    if current is not None:
        current.is_current = False
        current.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await db.flush()

    record = ProjectSourceProfile(
        project_id=source_version.project_id,
        source_id=source_version.source_id,
        source_version_id=source_version.id,
        schema_version=parsed.schema_version,
        version=next_version,
        is_current=True,
        source_fingerprint=fingerprint,
        payload=payload,
        status=status,
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        supersedes_id=current.id if current is not None else None,
    )
    db.add(record)
    await db.flush()
    return record


async def ensure_legacy_source_profile(db: AsyncSession, source_version: SourceVersion) -> ProjectSourceProfile:
    current = await load_current_source_profile(db, source_version.id)
    fingerprint = source_profile_fingerprint(source_version)
    if current is not None and current.source_fingerprint == fingerprint:
        return current
    return await save_source_profile(
        db,
        source_version=source_version,
        profile=legacy_source_profile(source_version),
        status=V2RecordStatus.READY,
    )


def _validate_profile_identity(profile: SourceProfile, source_version: SourceVersion) -> None:
    expected = {
        "project_id": str(source_version.project_id),
        "source_id": str(source_version.source_id),
        "source_version_id": str(source_version.id),
    }
    actual = {
        "project_id": profile.project_id,
        "source_id": profile.source_id,
        "source_version_id": profile.source_version_id,
    }
    mismatched = [field for field in expected if actual[field] != expected[field]]
    if mismatched:
        raise ValueError(f"source profile identity mismatch: {', '.join(mismatched)}")


def semantic_source_kind(value: Any) -> str:
    text = str(value or "").strip().casefold()
    representation_kinds = {str(item) for item in SourceRepresentationKind}
    if not text or text in representation_kinds:
        return "unknown"
    return text


__all__ = [
    "SOURCE_PROFILE_SCHEMA_VERSION",
    "ensure_legacy_source_profile",
    "legacy_source_profile",
    "load_current_source_profile",
    "resolve_source_profile",
    "save_source_profile",
    "semantic_source_kind",
    "source_profile_fingerprint",
]
