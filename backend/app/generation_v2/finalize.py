from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import ActDraft, ClaimCitationBundle, FinalTranscript, Treatment
from app.models.script_segment import AuditStatus, ScriptSegment
from app.pipeline.phase7_audio.shownotes import build_shownotes
from app.pipeline.phase7_audio.tts import generate_audio
from app.pipeline.state import PipelineState
from app.models.generation_run import GenerationRun


async def write_v2_script_segments(
    db: AsyncSession,
    run: GenerationRun,
    *,
    drafts: list[ActDraft],
) -> list[str]:
    await db.execute(delete(ScriptSegment).where(ScriptSegment.generation_run_id == run.id))
    segment_ids: list[str] = []
    for draft in drafts:
        segment = ScriptSegment(
            generation_run_id=run.id,
            act_number=draft.act_number,
            scene_number=1,
            dialogue_json=[line.model_dump(mode="json") for line in draft.lines],
            writer_original_dialogue_json=[line.model_dump(mode="json") for line in draft.lines],
            audit_status=AuditStatus.PASSED,
            revision_count=0,
            quality_metrics={"v2": True, "line_count": len(draft.lines)},
        )
        db.add(segment)
        await db.flush()
        segment_ids.append(str(segment.id))
    return segment_ids


async def generate_audio_from_segments(
    run: GenerationRun,
    *,
    script_segment_ids: list[str] | None = None,
    quality_metrics: dict | None = None,
) -> str:
    ids = script_segment_ids or await _load_script_segment_ids(run.id)
    if not ids:
        raise RuntimeError("Audio promotion requires script segments")

    state = _audio_state(run, ids, quality_metrics or {})
    audio_result = await generate_audio(state)
    episode_id = str(audio_result.get("episode_id") or "")
    if not episode_id:
        raise RuntimeError("Audio generation completed without episode id")
    await build_shownotes({**state, "episode_id": episode_id})
    return episode_id


def transcript_payload(treatment: Treatment, drafts: list[ActDraft], bundle: ClaimCitationBundle) -> FinalTranscript:
    from app.generation_v2.agents.editorial import final_transcript

    return final_transcript(
        run=None,  # type: ignore[arg-type]
        treatment=treatment,
        drafts=drafts,
        issues=bundle.fact_issues,
    )


async def _load_script_segment_ids(run_id: uuid.UUID) -> list[str]:
    from app.workers.worker_db import worker_session_factory

    async with worker_session_factory() as db:
        result = await db.execute(
            select(ScriptSegment.id)
            .where(ScriptSegment.generation_run_id == run_id)
            .order_by(ScriptSegment.act_number.asc(), ScriptSegment.scene_number.asc())
        )
        return [str(row[0]) for row in result.fetchall()]


def _audio_state(run: GenerationRun, script_segment_ids: list[str], quality_metrics: dict) -> PipelineState:
    config = run.config or {}
    return {
        "generation_run_id": str(run.id),
        "project_id": str(run.project_id),
        "user_id": str(run.user_id),
        "user_prompt": config.get("user_prompt", "Podcast Episode"),
        "duration_target": config.get("duration_target", 1800),
        "language": config.get("language", "en"),
        "show_bible_id": str(run.show_bible_id) if run.show_bible_id else "",
        "source_ids": [],
        "content_map_id": "",
        "manifest_id": "",
        "evidence_ids": [],
        "claim_ids": [],
        "conflict_brief_ids": [],
        "script_segment_ids": script_segment_ids,
        "episode_id": "",
        "current_phase": "v2_audio",
        "iteration_counts": {},
        "quality_signals": quality_metrics,
        "research_gaps": [],
        "rebuilt_claim_ids": [],
        "preserved_claim_ids": [],
        "targeted_branch_ids": [],
        "dedupe_source_claims": [],
        "dedupe_deleted_claim_ids": [],
        "dedupe_merged_into_claim_ids": [],
        "dedupe_untouched_claim_ids": [],
    }
