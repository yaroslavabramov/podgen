from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session_factory
from app.generation_v2.contracts import V2ToolName
from app.generation_v2.pipeline.triage_state_loader import load_passed_triage_state
from app.generation_v2.triage_eval_support import emit as _emit
from app.models.generation_run import GenerationRun, GenerationStatus
from app.models.generation_v2 import EvidenceItem, RunCorpusView, V2Artifact, V2ArtifactType, V2ToolTrace


async def clone_research_leaf_eval_run(
    *,
    source_run_id: uuid.UUID,
    question_id: str,
    max_cost_usd: float,
    model: str,
    reasoning_effort: str,
) -> GenerationRun:
    async with async_session_factory() as db:
        source = await db.get(GenerationRun, source_run_id)
        if source is None:
            raise RuntimeError(f"Source generation run {source_run_id} was not found")
        source_state = await load_passed_triage_state(db, source)
        source_view = source_state.corpus.run_view
        config = deepcopy(source.config or {})
        config["research_leaf_eval"] = {
            "source_run_id": str(source_run_id),
            "question_id": question_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "max_cost_usd": max_cost_usd,
            "isolation": "cloned_triage_baseline",
        }
        eval_run = GenerationRun(
            project_id=source.project_id,
            user_id=source.user_id,
            show_bible_id=source.show_bible_id,
            status=GenerationStatus.RUNNING,
            config=config,
            estimated_cost=source.estimated_cost,
            started_at=datetime.utcnow(),
            current_phase="v2_editorial_research",
            current_step=f"research leaf eval: {question_id}",
            progress_pct=0,
        )
        db.add(eval_run)
        await db.flush()
        eval_view = RunCorpusView(
            generation_run_id=eval_run.id,
            project_id=source_view.project_id,
            provider_index_id=source_view.provider_index_id,
            revision=source_view.revision,
            attempt=source_view.attempt,
            is_current=True,
            supersedes_id=None,
            selected_source_ids=deepcopy(source_view.selected_source_ids or []),
            selected_source_version_ids=deepcopy(source_view.selected_source_version_ids or []),
            active_source_filter=deepcopy(source_view.active_source_filter or {}),
            overlay_index_ids=deepcopy(source_view.overlay_index_ids or []),
            eligible_provider_file_ids=deepcopy(source_view.eligible_provider_file_ids or []),
            readiness_summary=deepcopy(source_view.readiness_summary or {}),
            manifest_hash=source_view.manifest_hash,
            status=source_view.status,
        )
        db.add(eval_view)
        await db.flush()

        evidence_id_map = await _clone_eval_evidence_items(
            db,
            source_run_id=source.id,
            eval_run_id=eval_run.id,
            revision=int(source_view.revision or 1),
            attempt=int(source_view.attempt or 1),
        )
        await _clone_eval_triage_artifacts(
            db,
            source_run_id=source.id,
            eval_run_id=eval_run.id,
            source_view_id=source_view.id,
            eval_view_id=eval_view.id,
            revision=int(source_view.revision or 1),
            attempt=int(source_view.attempt or 1),
            evidence_id_map=evidence_id_map,
        )
        await db.commit()
        await db.refresh(eval_run)
        _emit(
            "research_leaf_eval_started",
            run_id=str(eval_run.id),
            source_run_id=str(source_run_id),
            question_id=question_id,
            model=model,
            reasoning_effort=reasoning_effort,
            max_cost_usd=max_cost_usd,
            cloned_evidence_count=len(evidence_id_map),
        )
        return eval_run


async def _clone_eval_evidence_items(
    db: Any,
    *,
    source_run_id: uuid.UUID,
    eval_run_id: uuid.UUID,
    revision: int,
    attempt: int,
) -> dict[str, str]:
    result = await db.execute(
        select(EvidenceItem)
        .where(
            EvidenceItem.generation_run_id == source_run_id,
            EvidenceItem.corpus_revision == revision,
            EvidenceItem.attempt == attempt,
        )
        .order_by(EvidenceItem.created_at.asc())
    )
    evidence_items = list(result.scalars())
    evidence_id_map = {str(item.id): str(uuid.uuid4()) for item in evidence_items}
    for item in evidence_items:
        db.add(
            EvidenceItem(
                id=uuid.UUID(evidence_id_map[str(item.id)]),
                generation_run_id=eval_run_id,
                corpus_revision=item.corpus_revision,
                attempt=item.attempt,
                project_id=item.project_id,
                source_id=item.source_id,
                source_version_id=item.source_version_id,
                provider=item.provider,
                provider_vector_store_id=item.provider_vector_store_id,
                provider_file_id=item.provider_file_id,
                provider_vector_store_file_id=item.provider_vector_store_file_id,
                response_id=item.response_id,
                tool_call_id=item.tool_call_id,
                retrieval_channel=item.retrieval_channel,
                query_text=item.query_text,
                purpose=item.purpose,
                search_result_index=item.search_result_index,
                score=item.score,
                snippet_text=item.snippet_text,
                normalized_quote_text=item.normalized_quote_text,
                locator_text=item.locator_text,
                locator_type=item.locator_type,
                evidence_strength=item.evidence_strength,
                snippet_hash=item.snippet_hash,
                metadata_=rewrite_clone_payload(
                    item.metadata_ or {},
                    evidence_id_map=evidence_id_map,
                    artifact_id_map={},
                    source_run_id=source_run_id,
                    eval_run_id=eval_run_id,
                    source_view_id=None,
                    eval_view_id=None,
                ),
            )
        )
    return evidence_id_map


async def _clone_eval_triage_artifacts(
    db: Any,
    *,
    source_run_id: uuid.UUID,
    eval_run_id: uuid.UUID,
    source_view_id: uuid.UUID,
    eval_view_id: uuid.UUID,
    revision: int,
    attempt: int,
    evidence_id_map: dict[str, str],
) -> None:
    artifact_types = {
        V2ArtifactType.SOURCE_CARD,
        V2ArtifactType.QUOTE_CARD,
        V2ArtifactType.SOURCE_MAP,
        V2ArtifactType.SOURCE_TRIAGE,
        V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
        V2ArtifactType.CORPUS_BRIEF,
        V2ArtifactType.CORPUS_BRIEF_AUDIT,
        V2ArtifactType.EVIDENCE_ITEM,
    }
    result = await db.execute(
        select(V2Artifact)
        .where(
            V2Artifact.generation_run_id == source_run_id,
            V2Artifact.corpus_revision == revision,
            V2Artifact.attempt == attempt,
            V2Artifact.is_current.is_(True),
            V2Artifact.artifact_type.in_([item.value for item in artifact_types]),
        )
        .order_by(V2Artifact.created_at.asc())
    )
    artifacts = list(result.scalars())
    artifact_id_map = {str(item.id): str(uuid.uuid4()) for item in artifacts}
    for item in artifacts:
        db.add(
            V2Artifact(
                id=uuid.UUID(artifact_id_map[str(item.id)]),
                generation_run_id=eval_run_id,
                artifact_type=item.artifact_type,
                artifact_key=item.artifact_key,
                version=item.version,
                corpus_revision=item.corpus_revision,
                attempt=item.attempt,
                is_current=item.is_current,
                prompt_hash=item.prompt_hash,
                input_artifact_ids=rewrite_clone_payload(
                    item.input_artifact_ids or [],
                    evidence_id_map=evidence_id_map,
                    artifact_id_map=artifact_id_map,
                    source_run_id=source_run_id,
                    eval_run_id=eval_run_id,
                    source_view_id=source_view_id,
                    eval_view_id=eval_view_id,
                ),
                superseded_by_id=None,
                parent_artifact_ids=rewrite_clone_payload(
                    item.parent_artifact_ids or [],
                    evidence_id_map=evidence_id_map,
                    artifact_id_map=artifact_id_map,
                    source_run_id=source_run_id,
                    eval_run_id=eval_run_id,
                    source_view_id=source_view_id,
                    eval_view_id=eval_view_id,
                ),
                payload=rewrite_clone_payload(
                    item.payload or {},
                    evidence_id_map=evidence_id_map,
                    artifact_id_map=artifact_id_map,
                    source_run_id=source_run_id,
                    eval_run_id=eval_run_id,
                    source_view_id=source_view_id,
                    eval_view_id=eval_view_id,
                ),
                model=item.model,
                provider=item.provider,
                status=item.status,
            )
        )


def rewrite_clone_payload(
    value: Any,
    *,
    evidence_id_map: dict[str, str],
    artifact_id_map: dict[str, str],
    source_run_id: uuid.UUID,
    eval_run_id: uuid.UUID,
    source_view_id: uuid.UUID | None,
    eval_view_id: uuid.UUID | None,
) -> Any:
    if isinstance(value, dict):
        return {
            key: rewrite_clone_payload(
                item,
                evidence_id_map=evidence_id_map,
                artifact_id_map=artifact_id_map,
                source_run_id=source_run_id,
                eval_run_id=eval_run_id,
                source_view_id=source_view_id,
                eval_view_id=eval_view_id,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            rewrite_clone_payload(
                item,
                evidence_id_map=evidence_id_map,
                artifact_id_map=artifact_id_map,
                source_run_id=source_run_id,
                eval_run_id=eval_run_id,
                source_view_id=source_view_id,
                eval_view_id=eval_view_id,
            )
            for item in value
        ]
    if isinstance(value, str):
        if value in evidence_id_map:
            return evidence_id_map[value]
        if value in artifact_id_map:
            return artifact_id_map[value]
        if value == str(source_run_id):
            return str(eval_run_id)
        if source_view_id is not None and eval_view_id is not None and value == str(source_view_id):
            return str(eval_view_id)
    return value


async def research_leaf_trace_metrics(db: Any, run_id: uuid.UUID) -> dict[str, Any]:
    result = await db.execute(
        select(V2ToolTrace).where(V2ToolTrace.generation_run_id == run_id).order_by(V2ToolTrace.created_at.asc())
    )
    traces = list(result.scalars())
    model_call_traces = [trace for trace in traces if trace.tool_name == V2ToolName.OPENAI_RESPONSES_CREATE]
    requested_models = sorted(
        {
            str((trace.input_json or {}).get("requested_model") or (trace.output_json or {}).get("requested_model") or "")
            for trace in model_call_traces
            if str((trace.input_json or {}).get("requested_model") or (trace.output_json or {}).get("requested_model") or "")
        }
    )
    response_models = sorted(
        {
            str((trace.output_json or {}).get("response_model") or "")
            for trace in model_call_traces
            if str((trace.output_json or {}).get("response_model") or "")
        }
    )
    billing_models = sorted(
        {
            str((trace.output_json or {}).get("billing_model") or "")
            for trace in model_call_traces
            if str((trace.output_json or {}).get("billing_model") or "")
        }
    )
    latencies = [float(trace.latency_ms or 0.0) for trace in model_call_traces if trace.latency_ms is not None]
    return {
        "model_call_count": len(model_call_traces),
        "observed_models": {
            "requested": requested_models,
            "response": response_models,
            "billing": billing_models,
        },
        "latency_ms": {
            "total": round(sum(latencies), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
            "average": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        },
    }


__all__ = [
    "clone_research_leaf_eval_run",
    "research_leaf_trace_metrics",
    "rewrite_clone_payload",
]
