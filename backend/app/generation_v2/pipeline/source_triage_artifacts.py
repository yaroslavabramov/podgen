from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.corpus_brief_persistence import save_corpus_brief, save_corpus_brief_audit
from app.generation_v2.pipeline.artifact_persistence import save_evidence_artifacts
from app.generation_v2.runtime import V2Runtime
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType
from app.generation_v2.source_triage_canonical import audit_corpus_brief, materialize_corpus_brief


@dataclass(frozen=True)
class SourceTriageArtifactSet:
    source_map_artifact_id: str
    evidence_artifact_ids: list[str]
    source_artifact_ids: list[str]
    quote_artifact_ids: list[str]
    triage_artifact_id: str
    triage_gate_artifact_id: str
    corpus_brief: object
    corpus_brief_artifact_id: str
    corpus_brief_audit_artifact_id: str


async def save_source_triage_artifacts(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    source_map,
    triage_gate,
    *,
    triage_completion_artifact_id: str,
    has_vector_store: bool,
    workspace: dict,
    run_view,
) -> SourceTriageArtifactSet:
    source_map_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_MAP,
        artifact_key="main",
        payload=source_map,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_source_triage", "source_triage_finalizer_agent"),
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        prompt_hash=runtime.prompt_hash_for("v2_source_triage", "source_triage_finalizer_agent"),
        metadata=runtime.fallback_metadata_for("v2_source_triage", "source_triage_finalizer_agent"),
        input_artifact_ids=[triage_completion_artifact_id],
    )
    source_map_artifact_id = str(source_map_artifact.id)
    corpus_brief = materialize_corpus_brief(
        generation_run_id=str(run.id),
        project_id=str(run.project_id),
        run_corpus_view_id=str(run_view.id),
        corpus_revision=int(run_view.revision or 1),
        attempt=int(run_view.attempt or 1),
        source_map_artifact_id=source_map_artifact_id,
        source_map=source_map,
        workspace=workspace,
    )
    corpus_brief_artifact = await save_corpus_brief(
        db,
        generation_run_id=run.id,
        brief=corpus_brief,
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[triage_completion_artifact_id, source_map_artifact_id],
    )
    corpus_brief_audit = audit_corpus_brief(
        corpus_brief_artifact_id=str(corpus_brief_artifact.id),
        brief=corpus_brief,
        source_map=source_map,
        selected_source_ids={str(item) for item in run_view.selected_source_ids},
        expected_generation_run_id=str(run.id),
        expected_project_id=str(run.project_id),
        expected_run_corpus_view_id=str(run_view.id),
        expected_corpus_revision=int(run_view.revision or 1),
        expected_attempt=int(run_view.attempt or 1),
        expected_source_map_artifact_id=source_map_artifact_id,
    )
    corpus_brief_audit_artifact = await save_corpus_brief_audit(
        db,
        generation_run_id=run.id,
        report=corpus_brief_audit,
        corpus_revision=int(run_view.revision or 1),
        attempt=int(run_view.attempt or 1),
    )
    if triage_gate.passed and not corpus_brief_audit.passed:
        raise RuntimeError(f"CorpusBrief objective audit failed: {corpus_brief_audit.summary}")
    evidence_artifact_ids = await save_evidence_artifacts(
        db,
        run,
        evidence_ids=source_map.evidence_ids,
        input_artifact_ids=[source_map_artifact_id],
    )
    source_artifact_ids: list[str] = []
    quote_artifact_ids: list[str] = []
    for card in source_map.source_cards:
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.SOURCE_CARD,
            artifact_key=card.source_id,
            payload=card,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[source_map_artifact_id, *evidence_artifact_ids],
        )
        source_artifact_ids.append(str(artifact.id))
    for idx, quote in enumerate(source_map.quote_cards):
        artifact = await save_artifact(
            db,
            generation_run_id=run.id,
            artifact_type=V2ArtifactType.QUOTE_CARD,
            artifact_key=f"{quote.source_id}:{idx}",
            payload=quote,
            status=V2ArtifactStatus.PASSED,
            input_artifact_ids=[source_map_artifact_id, *evidence_artifact_ids],
        )
        quote_artifact_ids.append(str(artifact.id))
    triage_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="main",
        payload=source_map.source_triage,
        provider="openai" if has_vector_store else None,
        model=runtime.model_for("v2_source_triage", "source_triage_finalizer_agent"),
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[
            triage_completion_artifact_id,
            source_map_artifact_id,
            str(corpus_brief_artifact.id),
            str(corpus_brief_audit_artifact.id),
            *source_artifact_ids,
            *quote_artifact_ids,
        ],
        prompt_hash=runtime.prompt_hash_for("v2_source_triage", "source_triage_finalizer_agent"),
        metadata=runtime.fallback_metadata_for("v2_source_triage", "source_triage_finalizer_agent"),
    )
    triage_artifact_id = str(triage_artifact.id)
    triage_gate_artifact = await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="gate",
        payload=triage_gate,
        status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        input_artifact_ids=[
            triage_completion_artifact_id,
            source_map_artifact_id,
            triage_artifact_id,
            str(corpus_brief_artifact.id),
            str(corpus_brief_audit_artifact.id),
        ],
    )
    return SourceTriageArtifactSet(
        source_map_artifact_id=source_map_artifact_id,
        evidence_artifact_ids=evidence_artifact_ids,
        source_artifact_ids=source_artifact_ids,
        quote_artifact_ids=quote_artifact_ids,
        triage_artifact_id=triage_artifact_id,
        triage_gate_artifact_id=str(triage_gate_artifact.id),
        corpus_brief=corpus_brief,
        corpus_brief_artifact_id=str(corpus_brief_artifact.id),
        corpus_brief_audit_artifact_id=str(corpus_brief_audit_artifact.id),
    )
