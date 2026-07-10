from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import (
    ResearchAudit,
    ResearchMemo,
)
from app.generation_v2.pipeline.artifact_claim_persistence import (
    save_claim_bundle,
    save_evidence_artifact_map,
    save_evidence_artifacts,
    save_support_judgments,
    validate_and_save_claim_bundle,
)
from app.generation_v2.pipeline.artifact_story_persistence import (
    save_episode_critique_artifact,
    save_rewritten_act_draft_artifact,
    save_treatment_artifact,
    save_treatment_gate_artifact,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType



@dataclass(frozen=True)
class ResearchMemoArtifactSet:
    memo_artifact_id: str
    atom_artifact_ids: list[str]
    evidence_artifact_ids_by_evidence_id: dict[str, str]


async def save_research_memo_artifacts(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    memo: ResearchMemo,
    *,
    evidence_ids: list[str],
    source_workspace_artifact_ids: list[str],
    research_workspace_artifact_ids: list[str],
    has_vector_store: bool,
) -> ResearchMemoArtifactSet:
    evidence_artifact_ids_by_evidence_id = await save_evidence_artifact_map(
        db,
        run,
        evidence_ids=evidence_ids,
        input_artifact_ids=[*source_workspace_artifact_ids, *research_workspace_artifact_ids],
    )
    memo_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_MEMO,
        artifact_key="main",
        payload=memo,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_editorial_research", "research_finalizer_agent"),
        status=V2ArtifactStatus.PASSED,
        input_artifact_ids=[
            *source_workspace_artifact_ids,
            *research_workspace_artifact_ids,
            *evidence_artifact_ids_by_evidence_id.values(),
        ],
        prompt_hash=runtime.prompt_hash_for("v2_editorial_research", "research_finalizer_agent"),
        metadata=runtime.fallback_metadata_for("v2_editorial_research", "research_finalizer_agent"),
    )
    memo_artifact_id = str(memo_artifact.id)
    atom_artifact_ids: list[str] = []
    for idx, atom in enumerate(memo.editorial_atoms):
        atom_evidence_artifact_ids = [
            evidence_artifact_ids_by_evidence_id[evidence_id]
            for evidence_id in atom.evidence_ids
            if evidence_id in evidence_artifact_ids_by_evidence_id
        ]
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.EDITORIAL_ATOM,
            artifact_key=f"{idx + 1}:{atom.kind}",
            payload=atom,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[memo_artifact_id, *atom_evidence_artifact_ids],
            parent_artifact_ids=[memo_artifact_id],
        )
        atom_artifact_ids.append(str(artifact.id))
    return ResearchMemoArtifactSet(
        memo_artifact_id=memo_artifact_id,
        atom_artifact_ids=atom_artifact_ids,
        evidence_artifact_ids_by_evidence_id=evidence_artifact_ids_by_evidence_id,
    )


async def save_main_research_audit_artifact(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    audit: ResearchAudit,
    *,
    memo_artifact_id: str,
    atom_artifact_ids: list[str],
    memo_gate_artifact_id: str,
    has_vector_store: bool,
) -> str:
    artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_AUDIT,
        artifact_key="main",
        payload=audit,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_research_audit", "research_auditor"),
        status=V2ArtifactStatus.PASSED if audit.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[memo_artifact_id, *atom_artifact_ids, memo_gate_artifact_id],
        prompt_hash=runtime.prompt_hash_for("v2_research_audit", "research_auditor"),
        metadata=runtime.fallback_metadata_for("v2_research_audit", "research_auditor"),
    )
    return str(artifact.id)


async def save_memo_evidence_gate_artifact(
    db: AsyncSession,
    run: GenerationRun,
    memo_evidence_gate: ResearchAudit,
    *,
    memo_artifact_id: str,
    atom_artifact_ids: list[str],
    source_map_artifact_id: str,
) -> str:
    artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.RESEARCH_AUDIT,
        artifact_key="memo_evidence_gate",
        payload=memo_evidence_gate,
        status=V2ArtifactStatus.PASSED if memo_evidence_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[memo_artifact_id, *atom_artifact_ids, source_map_artifact_id],
    )
    return str(artifact.id)

__all__ = [
    "save_claim_bundle",
    "save_evidence_artifact_map",
    "save_evidence_artifacts",
    "save_support_judgments",
    "validate_and_save_claim_bundle",
    "save_episode_critique_artifact",
    "save_rewritten_act_draft_artifact",
    "save_treatment_artifact",
    "save_treatment_gate_artifact",
]
