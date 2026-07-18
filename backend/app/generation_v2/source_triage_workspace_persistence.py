from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.source_triage_workspace_state import normalize_workspace as _normalize_workspace, now as _now
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType


WORKSPACE_ARTIFACT_KEY = "main"


class SourceTriageWorkspaceConflict(RuntimeError):
    """A stale worker tried to overwrite newer durable triage state."""


async def _lock_workspace_run(db: AsyncSession, generation_run_id: uuid.UUID) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await db.execute(
        select(GenerationRun.id)
        .where(GenerationRun.id == generation_run_id)
        .with_for_update()
    )


async def _current_workspace_artifact(
    db: AsyncSession,
    generation_run_id: uuid.UUID,
) -> V2Artifact | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
            V2Artifact.artifact_key == WORKSPACE_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    current = list(result.scalars())
    if len(current) > 1:
        raise SourceTriageWorkspaceConflict(
            "source_triage_workspace_has_multiple_current_versions"
        )
    return current[0] if current else None


async def load_source_triage_workspace(db: AsyncSession, generation_run_id: uuid.UUID) -> dict[str, Any] | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
            V2Artifact.artifact_key == WORKSPACE_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        return None
    return _normalize_workspace(dict(artifact.payload or {}))


async def save_source_triage_workspace(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    workspace: dict[str, Any],
    status: str = V2ArtifactStatus.DRAFT,
    allow_scope_transition: bool = False,
) -> V2Artifact:
    caller_workspace = workspace
    workspace = _normalize_workspace(workspace)
    await _lock_workspace_run(db, generation_run_id)
    current = await _current_workspace_artifact(db, generation_run_id)
    actual_revision = int(((current.payload if current is not None else {}) or {}).get("_workspace_revision") or 0)
    expected_revision = int(workspace.get("_workspace_revision") or 0)
    current_scope = (
        int(((current.payload if current is not None else {}) or {}).get("corpus_revision") or 1),
        int(((current.payload if current is not None else {}) or {}).get("attempt") or 1),
    )
    incoming_scope = (
        int(workspace.get("corpus_revision") or 1),
        int(workspace.get("attempt") or 1),
    )
    if current is not None:
        if current_scope != incoming_scope:
            if not allow_scope_transition or incoming_scope <= current_scope:
                raise SourceTriageWorkspaceConflict(
                    "source_triage_workspace_scope_conflict: "
                    f"current={current_scope}, incoming={incoming_scope}; only the initializer may advance scope"
                )
        elif expected_revision != actual_revision:
            raise SourceTriageWorkspaceConflict(
                "source_triage_workspace_revision_conflict: "
                f"expected={expected_revision}, actual={actual_revision}; reload durable state and retry"
            )
    next_revision = actual_revision + 1
    workspace["_workspace_revision"] = next_revision
    workspace["updated_at"] = _now()
    artifact = await save_artifact(
        db,
        generation_run_id=generation_run_id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
        artifact_key=WORKSPACE_ARTIFACT_KEY,
        payload=workspace,
        status=status,
    )
    caller_workspace["_workspace_revision"] = next_revision
    caller_workspace["updated_at"] = workspace["updated_at"]
    return artifact


__all__ = [
    "SourceTriageWorkspaceConflict",
    "WORKSPACE_ARTIFACT_KEY",
    "load_source_triage_workspace",
    "save_source_triage_workspace",
]
