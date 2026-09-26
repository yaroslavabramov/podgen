from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import load_current_artifact
from app.generation_v2.pipeline.state import TriageState
from app.generation_v2.schemas import CorpusBrief, SourceMap, SourceTriageCompletion, SourceTriageGate
from app.generation_v2.source_corpus import load_v2_corpus_for_run_view
from app.generation_v2.source_corpus_run_view import load_run_corpus_view
from app.generation_v2.tools.evidence import EvidenceToolset, build_source_inventory
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType


async def load_passed_triage_state(
    db: AsyncSession,
    run: GenerationRun,
) -> TriageState:
    source_map_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.SOURCE_MAP,
        artifact_key="main",
    )
    if source_map_artifact is None:
        raise RuntimeError("V2 source triage SourceMap artifact is missing")
    revision = int(source_map_artifact.corpus_revision or 1)
    attempt = int(source_map_artifact.attempt or 1)
    run_view = await load_run_corpus_view(db, run.id, revision=revision, attempt=attempt)
    if run_view is None:
        raise RuntimeError("V2 source triage corpus view is missing for the artifact scope")
    corpus = await load_v2_corpus_for_run_view(db, run, run_view)

    triage_gate_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="gate",
        corpus_revision=revision,
        attempt=attempt,
    )
    triage_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="main",
        corpus_revision=revision,
        attempt=attempt,
    )
    completion_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE,
        artifact_key="completion",
        corpus_revision=revision,
        attempt=attempt,
    )
    corpus_brief_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.CORPUS_BRIEF,
        artifact_key="main",
        corpus_revision=revision,
        attempt=attempt,
    )
    corpus_brief_audit_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.CORPUS_BRIEF_AUDIT,
        artifact_key="main",
        corpus_revision=revision,
        attempt=attempt,
    )
    corpus_brief_shadow_audit_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.CORPUS_BRIEF_AUDIT,
        artifact_key="semantic_shadow",
        corpus_revision=revision,
        attempt=attempt,
    )
    if source_map_artifact is None or triage_gate_artifact is None or triage_artifact is None:
        raise RuntimeError("V2 source triage artifacts are missing for the current corpus attempt")
    if (
        source_map_artifact.status != V2ArtifactStatus.PASSED
        or triage_gate_artifact.status != V2ArtifactStatus.PASSED
        or triage_artifact.status != V2ArtifactStatus.PASSED
    ):
        raise RuntimeError("V2 source triage has not passed for the current corpus attempt")
    if (source_map_artifact.payload or {}).get("schema_version") == "source_map.v2":
        if (
            corpus_brief_artifact is None
            or corpus_brief_audit_artifact is None
            or corpus_brief_artifact.status != V2ArtifactStatus.PASSED
            or corpus_brief_audit_artifact.status != V2ArtifactStatus.PASSED
        ):
            raise RuntimeError("V2 CorpusBrief has not passed for the current corpus attempt")

    source_map = _source_map_from_artifact_payload(source_map_artifact.payload)
    triage_gate = SourceTriageGate.model_validate(_payload_without_metadata(triage_gate_artifact.payload))
    if not triage_gate.passed:
        raise RuntimeError("V2 source triage gate is not passed")
    if completion_artifact is not None:
        completion = SourceTriageCompletion.model_validate(_payload_without_metadata(completion_artifact.payload))
    else:
        completion = SourceTriageCompletion(ready=True, summary="Loaded passed source triage.", notes=[])
    if not completion.ready:
        # Old repair runs could persist the pre-repair completion. The backend
        # gate is authoritative when hydrating those historical attempts.
        completion = SourceTriageCompletion(
            ready=True,
            summary=completion.summary,
            notes=[*completion.notes, "Completion normalized from the passed backend gate."],
        )

    evidence_toolset = EvidenceToolset(
        db=db,
        run=run,
        run_view=corpus.run_view,
        provider_index=corpus.provider_index,
    )
    source_inventory = await build_source_inventory(db, corpus.run_view)
    workspace_artifact = await load_current_artifact(
        db,
        run.id,
        artifact_type=V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
        artifact_key="main",
        corpus_revision=revision,
        attempt=attempt,
    )
    related = await _current_related_artifacts(db, run, revision=revision, attempt=attempt)

    return TriageState(
        corpus=corpus,
        evidence_toolset=evidence_toolset,
        evidence_tools=evidence_toolset.agent_tools(),
        source_inventory=source_inventory,
        triage_completion=completion,
        source_map=source_map,
        triage_gate=triage_gate,
        source_cards=source_map.source_cards,
        quote_cards=source_map.quote_cards,
        source_map_artifact_id=str(source_map_artifact.id),
        evidence_artifact_ids=related[V2ArtifactType.EVIDENCE_ITEM],
        source_artifact_ids=related[V2ArtifactType.SOURCE_CARD],
        quote_artifact_ids=related[V2ArtifactType.QUOTE_CARD],
        workspace_artifact_id=str(workspace_artifact.id) if workspace_artifact else None,
        triage_artifact_id=str(triage_artifact.id),
        triage_gate_artifact_id=str(triage_gate_artifact.id),
        corpus_brief=(
            CorpusBrief.model_validate(_payload_without_metadata(corpus_brief_artifact.payload))
            if corpus_brief_artifact is not None
            else None
        ),
        corpus_brief_artifact_id=str(corpus_brief_artifact.id) if corpus_brief_artifact else None,
        corpus_brief_audit_artifact_id=(str(corpus_brief_audit_artifact.id) if corpus_brief_audit_artifact else None),
        corpus_brief_shadow_audit_artifact_id=(
            str(corpus_brief_shadow_audit_artifact.id) if corpus_brief_shadow_audit_artifact else None
        ),
    )


async def _current_related_artifacts(
    db: AsyncSession,
    run: GenerationRun,
    *,
    revision: int,
    attempt: int,
) -> dict[str, list[str]]:
    wanted = {
        V2ArtifactType.EVIDENCE_ITEM,
        V2ArtifactType.SOURCE_CARD,
        V2ArtifactType.QUOTE_CARD,
    }
    result = await db.execute(
        select(V2Artifact.id, V2Artifact.artifact_type).where(
            V2Artifact.generation_run_id == run.id,
            V2Artifact.corpus_revision == revision,
            V2Artifact.attempt == attempt,
            V2Artifact.is_current.is_(True),
            V2Artifact.artifact_type.in_(list(wanted)),
        )
    )
    grouped = {str(artifact_type): [] for artifact_type in wanted}
    for artifact_id, artifact_type in result.all():
        grouped[str(artifact_type)].append(str(artifact_id))
    return grouped


def _payload_without_metadata(payload: dict | None) -> dict:
    return {key: value for key, value in (payload or {}).items() if key != "_metadata"}


def _source_map_from_artifact_payload(payload: dict | None) -> SourceMap:
    return SourceMap.model_validate(_normalize_legacy_source_map_lineage(_payload_without_metadata(payload)))


def _normalize_legacy_source_map_lineage(payload: dict) -> dict:
    """Read-compatible fix for source-mapping artifacts saved before assessment lineage was canonicalized."""

    normalized = deepcopy(payload)
    assessments = [item for item in normalized.get("source_assessments") or [] if isinstance(item, dict)]
    analyses = [item for item in normalized.get("source_analyses") or [] if isinstance(item, dict)]
    assessment_id_by_source = {
        str(item.get("source_id") or ""): str(item.get("assessment_id") or "")
        for item in assessments
        if str(item.get("source_id") or "") and str(item.get("assessment_id") or "")
    }
    source_id_by_analysis_id = {
        str(item.get("analysis_id") or ""): str(item.get("source_id") or "")
        for item in analyses
        if str(item.get("analysis_id") or "") and str(item.get("source_id") or "")
    }
    if not assessment_id_by_source or not source_id_by_analysis_id:
        return normalized
    for finding in normalized.get("source_findings") or []:
        if not isinstance(finding, dict):
            continue
        assessment_id = str(finding.get("assessment_id") or "")
        source_id = str(finding.get("source_id") or "")
        if not assessment_id.startswith("analysis:"):
            continue
        if source_id_by_analysis_id.get(assessment_id) != source_id:
            continue
        canonical_assessment_id = assessment_id_by_source.get(source_id)
        if canonical_assessment_id:
            finding["assessment_id"] = canonical_assessment_id
    return normalized


__all__ = ["load_passed_triage_state"]
