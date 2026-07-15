from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.schemas_source import CorpusBrief, CorpusBriefAuditReport
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType


CORPUS_BRIEF_ARTIFACT_KEY = "main"
CORPUS_BRIEF_AUDIT_ARTIFACT_KEY = "main"


async def load_corpus_brief_artifact(db: AsyncSession, generation_run_id: uuid.UUID) -> V2Artifact | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.CORPUS_BRIEF,
            V2Artifact.artifact_key == CORPUS_BRIEF_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def load_corpus_brief(db: AsyncSession, generation_run_id: uuid.UUID) -> CorpusBrief | None:
    artifact = await load_corpus_brief_artifact(db, generation_run_id)
    if artifact is None:
        return None
    return CorpusBrief.model_validate(artifact.payload)


async def save_corpus_brief(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    brief: CorpusBrief | dict,
    status: str = V2ArtifactStatus.DRAFT,
    input_artifact_ids: list[str] | None = None,
    parent_artifact_ids: list[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_hash: str | None = None,
) -> V2Artifact:
    parsed = brief if isinstance(brief, CorpusBrief) else CorpusBrief.model_validate(brief)
    if parsed.generation_run_id != str(generation_run_id):
        raise ValueError("corpus brief generation_run_id does not match persistence scope")
    return await save_artifact(
        db,
        generation_run_id=generation_run_id,
        artifact_type=V2ArtifactType.CORPUS_BRIEF,
        artifact_key=CORPUS_BRIEF_ARTIFACT_KEY,
        payload=parsed,
        status=status,
        input_artifact_ids=input_artifact_ids or [parsed.source_map_artifact_id],
        parent_artifact_ids=parent_artifact_ids,
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        corpus_revision=parsed.corpus_revision,
        attempt=parsed.attempt,
    )


async def load_corpus_brief_audit(
    db: AsyncSession,
    generation_run_id: uuid.UUID,
) -> CorpusBriefAuditReport | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.CORPUS_BRIEF_AUDIT,
            V2Artifact.artifact_key == CORPUS_BRIEF_AUDIT_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        return None
    return CorpusBriefAuditReport.model_validate(artifact.payload)


async def save_corpus_brief_audit(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    report: CorpusBriefAuditReport | dict,
    corpus_revision: int,
    attempt: int,
    parent_artifact_ids: list[str] | None = None,
    artifact_key: str = CORPUS_BRIEF_AUDIT_ARTIFACT_KEY,
) -> V2Artifact:
    parsed = report if isinstance(report, CorpusBriefAuditReport) else CorpusBriefAuditReport.model_validate(report)
    return await save_artifact(
        db,
        generation_run_id=generation_run_id,
        artifact_type=V2ArtifactType.CORPUS_BRIEF_AUDIT,
        artifact_key=artifact_key,
        payload=parsed,
        status=V2ArtifactStatus.PASSED if parsed.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[parsed.corpus_brief_artifact_id, parsed.source_map_artifact_id],
        parent_artifact_ids=parent_artifact_ids,
        corpus_revision=corpus_revision,
        attempt=attempt,
    )


__all__ = [
    "CORPUS_BRIEF_ARTIFACT_KEY",
    "CORPUS_BRIEF_AUDIT_ARTIFACT_KEY",
    "load_corpus_brief",
    "load_corpus_brief_artifact",
    "load_corpus_brief_audit",
    "save_corpus_brief",
    "save_corpus_brief_audit",
]
