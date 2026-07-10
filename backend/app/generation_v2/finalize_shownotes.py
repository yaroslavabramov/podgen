from __future__ import annotations

import uuid

from sqlalchemy import select

from app.generation_v2.artifacts import list_artifacts_grouped
from app.generation_v2.schemas import ClaimCitationBundle, FinalTranscript, SourceCard
from app.models.episode import Episode


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
                        "locator": honest_locator(link, evidence_by_id.get(str(link.evidence_id or ""))),
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
    final_payload = first_payload(artifacts, "final_transcript")
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
                        "locator": honest_locator_payload(link, evidence.get(str(link.get("evidence_id") or ""))),
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


def first_payload(artifacts: dict, artifact_type: str) -> dict | None:
    items = artifacts.get(artifact_type) or []
    return items[0].get("payload") if items else None


def honest_locator(link, evidence: dict | None) -> str:
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


def honest_locator_payload(link: dict, evidence: dict | None) -> str:
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


async def store_v2_shownotes(episode_id: str, shownotes_json: dict, quality_metrics: dict) -> None:
    from app.workers.worker_db import worker_session_factory

    async with worker_session_factory() as db:
        result = await db.execute(select(Episode).where(Episode.id == uuid.UUID(episode_id)))
        episode = result.scalar_one_or_none()
        if episode is None:
            raise RuntimeError("V2 shownotes could not find episode generated by audio phase")
        episode.shownotes_json = shownotes_json
        episode.quality_metrics = quality_metrics
        await db.commit()


__all__ = [
    "build_v2_shownotes",
    "build_v2_shownotes_from_artifacts",
    "first_payload",
    "honest_locator",
    "honest_locator_payload",
    "store_v2_shownotes",
]
