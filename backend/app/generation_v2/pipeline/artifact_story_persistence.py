from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import ActDraft, EpisodeCritique, Treatment
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType


async def save_treatment_artifact(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    treatment: Treatment,
    treatment_gate: EpisodeCritique,
    *,
    memo_artifact_id: str,
    audit_artifact_id: str,
    atom_artifact_ids: list[str],
    has_vector_store: bool,
) -> str:
    artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.TREATMENT,
        artifact_key="main",
        payload=treatment,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_treatment", "story_editor"),
        status=V2ArtifactStatus.PASSED if treatment_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[memo_artifact_id, audit_artifact_id, *atom_artifact_ids],
        prompt_hash=runtime.prompt_hash_for("v2_treatment", "story_editor"),
        metadata=runtime.fallback_metadata_for("v2_treatment", "story_editor"),
    )
    return str(artifact.id)


async def save_treatment_gate_artifact(
    db: AsyncSession,
    run: GenerationRun,
    treatment_gate: EpisodeCritique,
    *,
    treatment_artifact_id: str,
    source_map_artifact_id: str,
    source_artifact_ids: list[str],
) -> str:
    artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_AUDIT,
        artifact_key="treatment_gate",
        payload=treatment_gate,
        status=V2ArtifactStatus.FAILED,
        input_artifact_ids=[treatment_artifact_id, source_map_artifact_id, *source_artifact_ids],
    )
    return str(artifact.id)


async def save_episode_critique_artifact(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    critique: EpisodeCritique,
    *,
    treatment_artifact_id: str,
    draft_artifact_ids_by_act: dict[int, str],
    source_artifact_ids: list[str],
    quote_artifact_ids: list[str],
    has_vector_store: bool,
) -> str:
    artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.EPISODE_CRITIQUE,
        artifact_key="main",
        payload=critique,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_critique_rewrite", "episode_critic"),
        status=V2ArtifactStatus.PASSED if critique.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[
            treatment_artifact_id,
            *draft_artifact_ids_by_act.values(),
            *source_artifact_ids,
            *quote_artifact_ids,
        ],
        prompt_hash=runtime.prompt_hash_for("v2_critique_rewrite", "episode_critic"),
        metadata=runtime.fallback_metadata_for("v2_critique_rewrite", "episode_critic"),
    )
    return str(artifact.id)


async def save_rewritten_act_draft_artifact(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    draft: ActDraft,
    *,
    treatment_act_number: int,
    critique_artifact_id: str,
    treatment_artifact_id: str,
    draft_artifact_id: str | None,
    dossier_artifact_id: str | None,
    bundle_artifact_ids: list[str] | None = None,
    has_vector_store: bool,
) -> str:
    input_artifact_ids = [
        critique_artifact_id,
        treatment_artifact_id,
        draft_artifact_id or "",
        dossier_artifact_id or "",
        *(bundle_artifact_ids or []),
    ]
    artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.ACT_DRAFT,
        artifact_key=f"act-{draft.act_number}",
        payload=draft,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_critique_rewrite", f"act_rewriter_{treatment_act_number}"),
        status=V2ArtifactStatus.PASSED,
        input_artifact_ids=[artifact_id for artifact_id in input_artifact_ids if artifact_id],
        prompt_hash=runtime.prompt_hash_for("v2_critique_rewrite", f"act_rewriter_{treatment_act_number}"),
        metadata=runtime.fallback_metadata_for(
            "v2_critique_rewrite",
            f"act_rewriter_{treatment_act_number}",
        ),
    )
    return str(artifact.id)


__all__ = [
    "save_episode_critique_artifact",
    "save_rewritten_act_draft_artifact",
    "save_treatment_artifact",
    "save_treatment_gate_artifact",
]
