from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import list_artifacts_grouped
from app.generation_v2.schemas import ActDraft, ClaimCitationBundle, FinalTranscript, SourceCard, Treatment
from app.models.script_segment import AuditStatus, ScriptSegment
from app.models.episode import Episode
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
    shownotes_json: dict | None = None,
) -> str:
    ids = script_segment_ids or await _load_script_segment_ids(run.id)
    if not ids:
        raise RuntimeError("Audio promotion requires script segments")

    state = _audio_state(run, ids, quality_metrics or {})
    audio_result = await generate_audio(state)
    episode_id = str(audio_result.get("episode_id") or "")
    if not episode_id:
        raise RuntimeError("Audio generation completed without episode id")
    if shownotes_json is None and _uses_v2_shownotes(run):
        shownotes_json = await build_v2_shownotes_from_artifacts(run.id)
    if shownotes_json:
        await _store_v2_shownotes(episode_id, shownotes_json, quality_metrics or {})
    else:
        await build_shownotes({**state, "episode_id": episode_id})
    return episode_id


def transcript_payload(
    treatment: Treatment,
    drafts: list[ActDraft],
    bundle: ClaimCitationBundle,
    source_cards: list[SourceCard] | None = None,
) -> FinalTranscript:
    from app.generation_v2.agents.editorial import final_transcript

    return final_transcript(
        run=None,  # type: ignore[arg-type]
        treatment=treatment,
        drafts=drafts,
        issues=bundle.fact_issues,
        bundle=bundle,
        source_cards=source_cards or [],
    )


def build_v2_shownotes(
    *,
    final: FinalTranscript,
    bundle: ClaimCitationBundle,
    source_cards: list[SourceCard],
    evidence_records: list[dict] | None = None,
) -> dict:
    sources_by_id = {source.source_id: source for source in source_cards}
    evidence_by_id = {str(item.get("id")): item for item in evidence_records or []}
    links_by_claim: dict[str, list] = {}
    for link in bundle.citation_links:
        links_by_claim.setdefault(link.claim_id, []).append(link)
    sections = []
    references = []
    for claim in bundle.claims:
        claim_links = links_by_claim.get(claim.claim_id, [])
        references.append(
            {
                "claim": claim.statement,
                "verification": claim.support_level.upper(),
                "act_number": claim.act_number,
                "risk_level": claim.risk_level,
                "sources": [
                    {
                        "title": sources_by_id.get(link.source_id).title if sources_by_id.get(link.source_id) else link.source_id,
                        "url": sources_by_id.get(link.source_id).url if sources_by_id.get(link.source_id) else "",
                        "quote": link.quote or "",
                        "locator": _honest_locator(link, evidence_by_id.get(str(link.evidence_id or ""))),
                        "evidence_id": link.evidence_id,
                        "source_version_id": link.source_version_id,
                        "retrieval_channel": (evidence_by_id.get(str(link.evidence_id or "")) or {}).get("retrieval_channel"),
                        "evidence_strength": (evidence_by_id.get(str(link.evidence_id or "")) or {}).get("evidence_strength"),
                    }
                    for link in claim_links[:4]
                ],
            }
        )
    sections.append({"title": final.title, "references": references, "conflicts": []})
    return {"sections": sections, "v2": True, "quality_metrics": final.quality_metrics}


async def build_v2_shownotes_from_artifacts(run_id: uuid.UUID) -> dict | None:
    from app.workers.worker_db import worker_session_factory

    async with worker_session_factory() as db:
        artifacts = await list_artifacts_grouped(db, run_id)
    final_payload = _first_payload(artifacts, "final_transcript")
    if not final_payload:
        return None
    references = []
    sources = {item["payload"]["source_id"]: item["payload"] for item in artifacts.get("source_card", []) if item.get("payload")}
    evidence = {item["payload"]["id"]: item["payload"] for item in artifacts.get("evidence_item", []) if item.get("payload")}
    links_by_claim: dict[str, list[dict]] = {}
    for item in artifacts.get("citation_link", []):
        payload = item.get("payload") or {}
        links_by_claim.setdefault(str(payload.get("claim_id")), []).append(payload)
    for item in artifacts.get("draft_claim", []):
        claim = item.get("payload") or {}
        claim_links = links_by_claim.get(str(claim.get("claim_id")), [])
        references.append(
            {
                "claim": claim.get("statement", ""),
                "verification": str(claim.get("support_level", "weak")).upper(),
                "act_number": claim.get("act_number"),
                "risk_level": claim.get("risk_level"),
                "sources": [
                    {
                        "title": (sources.get(link.get("source_id")) or {}).get("title", link.get("source_id", "")),
                        "url": (sources.get(link.get("source_id")) or {}).get("url", ""),
                        "quote": link.get("quote") or "",
                        "locator": _honest_locator_payload(link, evidence.get(str(link.get("evidence_id") or ""))),
                        "evidence_id": link.get("evidence_id"),
                        "source_version_id": link.get("source_version_id"),
                        "retrieval_channel": (evidence.get(str(link.get("evidence_id") or "")) or {}).get("retrieval_channel"),
                        "evidence_strength": (evidence.get(str(link.get("evidence_id") or "")) or {}).get("evidence_strength"),
                    }
                    for link in claim_links[:4]
                ],
            }
        )
    return {
        "sections": [{"title": final_payload.get("title", "Episode"), "references": references, "conflicts": []}],
        "v2": True,
        "quality_metrics": final_payload.get("quality_metrics", {}),
    }


def _first_payload(artifacts: dict, artifact_type: str) -> dict | None:
    items = artifacts.get(artifact_type) or []
    return items[0].get("payload") if items else None


def _uses_v2_shownotes(run: GenerationRun) -> bool:
    return (run.config or {}).get("generation_mode") == "v2"


def _honest_locator(link, evidence: dict | None) -> str:
    if evidence:
        locator = str(evidence.get("locator_text") or "").strip()
        locator_type = str(evidence.get("locator_type") or "").strip()
        if locator and locator_type and locator_type != "unknown":
            return f"{locator_type}: {locator}"
        if locator:
            return locator
        if evidence.get("snippet_text"):
            return "provider snippet"
    return link.locator


def _honest_locator_payload(link: dict, evidence: dict | None) -> str:
    if evidence:
        locator = str(evidence.get("locator_text") or "").strip()
        locator_type = str(evidence.get("locator_type") or "").strip()
        if locator and locator_type and locator_type != "unknown":
            return f"{locator_type}: {locator}"
        if locator:
            return locator
        if evidence.get("snippet_text"):
            return "provider snippet"
    return str(link.get("locator") or "")


async def _store_v2_shownotes(episode_id: str, shownotes_json: dict, quality_metrics: dict) -> None:
    from app.workers.worker_db import worker_session_factory

    async with worker_session_factory() as db:
        result = await db.execute(select(Episode).where(Episode.id == uuid.UUID(episode_id)))
        episode = result.scalar_one_or_none()
        if episode is None:
            raise RuntimeError("V2 shownotes could not find episode generated by audio phase")
        episode.shownotes_json = shownotes_json
        episode.quality_metrics = quality_metrics
        await db.commit()


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
