from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.citation_validation import validation_quality_metrics
from app.generation_v2.contracts.pipeline import V2PipelineStep
from app.generation_v2.finalize import (
    build_v2_shownotes,
    generate_audio_from_segments,
    transcript_payload,
    write_v2_script_segments,
)
from app.generation_v2.pipeline.status import commit_step, start_phase
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import ActDraft, CitationValidationReport, ClaimCitationBundle, SourceCard, Treatment
from app.generation_v2.source_corpus import list_evidence_dicts
from app.generation_v2.tools.evidence import mark_cited_evidence
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType


async def finalize_generation_outputs(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    treatment: Treatment,
    drafts: list[ActDraft],
    bundle: ClaimCitationBundle,
    source_cards: list[SourceCard],
    treatment_artifact_id: str,
    draft_artifact_ids_by_act: dict[int, str],
    bundle_artifact_ids: list[str],
    validation_artifact_id: str,
    validation_report: CitationValidationReport,
    generate_audio: bool,
) -> str | None:
    await mark_cited_evidence(
        db,
        generation_run_id=run.id,
        evidence_ids=[link.evidence_id for link in bundle.citation_links if link.evidence_id],
    )

    final = transcript_payload(treatment, drafts, bundle, source_cards=source_cards)
    final.quality_metrics.update(validation_quality_metrics(validation_report))
    final.quality_metrics["runtime_fallback_used"] = runtime.fallback_used
    await save_artifact(
        db,
        generation_run_id=run.id,
        artifact_type=V2ArtifactType.FINAL_TRANSCRIPT,
        artifact_key="main",
        payload=final,
        status=V2ArtifactStatus.PASSED,
        input_artifact_ids=[
            treatment_artifact_id,
            *draft_artifact_ids_by_act.values(),
            *bundle_artifact_ids,
            validation_artifact_id,
        ],
    )
    if runtime.fallback_used and generate_audio and not settings.generation_v2_allow_fallback_audio:
        await db.commit()
        raise RuntimeError("V2 runtime fallback was used; audio generation is disabled for fallback runs")
    segment_ids = await write_v2_script_segments(db, run, drafts=drafts)
    await commit_step(run, db, V2PipelineStep.FACT_CITATION, 88, next_step=V2PipelineStep.AUDIO if generate_audio else None)

    if not generate_audio:
        return None

    await start_phase(run, db, "v2_audio", "Generating audio from V2 transcript", 90)
    await db.commit()
    shownotes = build_v2_shownotes(
        final=final,
        bundle=bundle,
        source_cards=source_cards,
        evidence_records=await list_evidence_dicts(db, run.id),
    )
    episode_id = await generate_audio_from_segments(
        run,
        script_segment_ids=segment_ids,
        quality_metrics=final.quality_metrics,
        shownotes_json=shownotes,
    )
    await commit_step(run, db, V2PipelineStep.AUDIO, 100, next_step=None)
    return episode_id
