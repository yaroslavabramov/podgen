from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.source_triage_workspace_state import normalize_workspace as _normalize_workspace, now as _now
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType


WORKSPACE_ARTIFACT_KEY = "main"


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
    return dict(artifact.payload or {})


async def save_source_triage_workspace(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    workspace: dict[str, Any],
    status: str = V2ArtifactStatus.DRAFT,
) -> V2Artifact:
    workspace = _normalize_workspace(workspace)
    workspace["updated_at"] = _now()
    return await save_artifact(
        db,
        generation_run_id=generation_run_id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
        artifact_key=WORKSPACE_ARTIFACT_KEY,
        payload=workspace,
        status=status,
    )
