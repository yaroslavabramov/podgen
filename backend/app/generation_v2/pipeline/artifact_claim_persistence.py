from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import judge_claim_support
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.citation_validation import merge_validation_issues, validate_citations
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.pipeline.artifact_claim_bundle import save_claim_bundle
from app.generation_v2.pipeline.validation import dedupe_strings
from app.generation_v2.schemas import (
    CitationValidationReport,
    ClaimCitationBundle,
    QuoteCard,
    SourceCard,
    SupportJudgmentBundle,
)
from app.generation_v2.source_corpus import list_evidence_dicts
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import RunCorpusView, V2ArtifactStatus, V2ArtifactType


async def validate_and_save_claim_bundle(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    bundle: ClaimCitationBundle,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    run_view: RunCorpusView,
    evidence_tools,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    pass_key: str,
    metadata: dict | None = None,
) -> tuple[ClaimCitationBundle, list[str], CitationValidationReport, str]:
    evidence_records = await list_evidence_dicts(db, run.id)
    linked_evidence_ids = [link.evidence_id for link in bundle.citation_links if link.evidence_id]
    evidence_artifact_ids = await save_evidence_artifacts(
        db,
        run,
        evidence_ids=linked_evidence_ids,
        input_artifact_ids=input_artifact_ids,
    )
    support_bundle = await judge_claim_support(
        db,
        run,
        runtime,
        bundle=bundle,
        evidence_records=evidence_records,
        evidence_tools=evidence_tools,
    )
    support_artifact_ids = await save_support_judgments(
        db,
        run,
        support_bundle,
        input_artifact_ids=[*input_artifact_ids, *evidence_artifact_ids],
        prompt_hash=runtime.prompt_hash_for("v2_fact_citation", "support_judge"),
        model=runtime.model_for("v2_fact_citation", "support_judge"),
        metadata=runtime.fallback_metadata_for("v2_fact_citation", "support_judge"),
    )
    validation_report = await validate_citations(
        db,
        bundle=bundle,
        source_cards=source_cards,
        quote_cards=quote_cards,
        evidence_records=evidence_records,
        support_judgments=support_bundle.judgments,
        run_view=run_view,
    )
    bundle = merge_validation_issues(bundle, validation_report)
    bundle_artifact_ids = await save_claim_bundle(
        db,
        run,
        bundle,
        input_artifact_ids=[*input_artifact_ids, *evidence_artifact_ids, *support_artifact_ids],
        prompt_hash=prompt_hash,
        model=runtime.model_for("v2_fact_citation", "citation_linker"),
        metadata=metadata,
    )
    bundle_artifact_ids = [*evidence_artifact_ids, *support_artifact_ids, *bundle_artifact_ids]
    validation_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.CITATION_VALIDATION,
        artifact_key=pass_key,
        payload=validation_report,
        status=V2ArtifactStatus.PASSED if validation_report.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[*input_artifact_ids, *bundle_artifact_ids],
        model=runtime.model_for("v2_fact_citation", "citation_linker"),
        prompt_hash=prompt_hash,
        metadata=metadata,
    )
    return bundle, bundle_artifact_ids, validation_report, str(validation_artifact.id)


async def save_support_judgments(
    db: AsyncSession,
    run: GenerationRun,
    support_bundle: SupportJudgmentBundle,
    *,
    input_artifact_ids: list[str],
    prompt_hash: str | None,
    model: str | None = None,
    metadata: dict | None = None,
) -> list[str]:
    artifact_ids: list[str] = []
    for idx, judgment in enumerate(support_bundle.judgments):
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SUPPORT_JUDGMENT,
            artifact_key=f"{judgment.claim_id}:{judgment.evidence_id}:{idx}",
            payload=judgment,
            status=V2ArtifactStatus.PASSED if judgment.supported and not judgment.blocking else V2ArtifactStatus.FAILED,
            input_artifact_ids=input_artifact_ids,
            model=model,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        artifact_ids.append(str(artifact.id))
    return artifact_ids


async def save_evidence_artifacts(
    db: AsyncSession,
    run: GenerationRun,
    *,
    evidence_ids: list[str],
    input_artifact_ids: list[str],
) -> list[str]:
    return list(
        (
            await save_evidence_artifact_map(
                db,
                run,
                evidence_ids=evidence_ids,
                input_artifact_ids=input_artifact_ids,
            )
        ).values()
    )


async def save_evidence_artifact_map(
    db: AsyncSession,
    run: GenerationRun,
    *,
    evidence_ids: list[str],
    input_artifact_ids: list[str],
) -> dict[str, str]:
    if not evidence_ids:
        return {}
    evidence_by_id = {item["id"]: item for item in await list_evidence_dicts(db, run.id)}
    artifact_ids_by_evidence_id: dict[str, str] = {}
    for evidence_id in dedupe_strings(evidence_ids):
        payload = evidence_by_id.get(evidence_id)
        if not payload:
            continue
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.EVIDENCE_ITEM,
            artifact_key=evidence_id,
            payload=payload,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=input_artifact_ids,
        )
        artifact_ids_by_evidence_id[evidence_id] = str(artifact.id)
    return artifact_ids_by_evidence_id

__all__ = [
    "save_claim_bundle",
]
