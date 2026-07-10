from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.schemas import ClaimCitationBundle
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType


async def save_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    bundle: ClaimCitationBundle,
    *,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    model: str | None = None,
    metadata: dict | None = None,
) -> list[str]:
    artifact_ids: list[str] = []
    claim_artifact_ids_by_claim: dict[str, str] = {}
    for claim in bundle.claims:
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.DRAFT_CLAIM,
            artifact_key=claim.claim_id,
            payload=claim,
            status=V2ArtifactStatus.PASSED if claim.support_level != "unsupported" else V2ArtifactStatus.FAILED,
            input_artifact_ids=input_artifact_ids,
            model=model,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_id = str(artifact.id)
        artifact_ids.append(artifact_id)
        claim_artifact_ids_by_claim[claim.claim_id] = artifact_id
    for idx, link in enumerate(bundle.citation_links):
        link_inputs = [
            *input_artifact_ids,
            *([claim_artifact_ids_by_claim[link.claim_id]] if link.claim_id in claim_artifact_ids_by_claim else []),
        ]
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.CITATION_LINK,
            artifact_key=f"{link.claim_id}:{idx}",
            payload=link,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=link_inputs,
            model=model,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    for issue in bundle.fact_issues:
        issue_inputs = [
            *input_artifact_ids,
            *([claim_artifact_ids_by_claim[issue.claim_id]] if issue.claim_id in claim_artifact_ids_by_claim else []),
        ]
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.FACT_ISSUE,
            artifact_key=issue.claim_id,
            payload=issue,
            status=V2ArtifactStatus.FAILED,
            input_artifact_ids=issue_inputs,
            model=model,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    return artifact_ids


__all__ = ["save_claim_bundle"]
