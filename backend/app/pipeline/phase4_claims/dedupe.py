"""Phase 4.15: deterministic global claim dedupe before verification."""

import json
import logging
import re
import uuid
from collections import defaultdict

from sqlalchemy import select

from app.models.claim import Claim, ClaimEvidence
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.source import Source
from app.pipeline.phase3_research.normalization import normalize_research_text
from app.pipeline.state import PipelineState
from app.realtime.events import progress, step_completed, step_started
from app.realtime.publisher import publish_event, update_generation_progress
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "that", "this",
    "is", "was", "were", "it", "as", "at", "by", "from", "with", "about", "what",
    "how", "why", "when", "who", "which", "что", "как", "это", "для", "при", "под",
    "the", "der", "die", "das", "und", "mit", "von",
}


async def dedupe_claims(state: PipelineState) -> dict:
    run_id = state["generation_run_id"]
    claim_ids = list(dict.fromkeys(state.get("claim_ids", [])))
    if not claim_ids:
        return {"claim_ids": claim_ids, "current_phase": "phase4_claims"}

    publish_event(run_id, step_started("phase4_claims", "dedupe_claims", "Collapsing duplicate claims"))
    update_generation_progress(run_id, "phase4_claims", "claim_dedupe", 41.0)

    async with async_session_factory() as db:
        claims, contexts = await _load_claim_context(db, claim_ids, run_id)
        duplicate_map: dict[str, str] = {}
        ordered_ids = [str(claim.id) for claim in claims]

        for idx, claim in enumerate(claims):
            claim_id = str(claim.id)
            if claim_id in duplicate_map:
                continue
            for candidate in claims[idx + 1:]:
                candidate_id = str(candidate.id)
                if candidate_id in duplicate_map:
                    continue
                if _claims_match(contexts[claim_id], contexts[candidate_id]):
                    await _merge_claims(db, keep_claim_id=claim.id, drop_claim_id=candidate.id)
                    duplicate_map[candidate_id] = claim_id

        if duplicate_map:
            for dropped_id in duplicate_map:
                dropped = next((claim for claim in claims if str(claim.id) == dropped_id), None)
                if dropped is not None:
                    await db.delete(dropped)
            await db.commit()

        kept_claim_ids = [cid for cid in ordered_ids if cid not in duplicate_map]

    publish_event(run_id, step_completed("phase4_claims", "dedupe_claims"))
    publish_event(
        run_id,
        progress(42.0, f"Collapsed {len(claim_ids) - len(kept_claim_ids)} duplicate claims"),
    )
    return {"claim_ids": kept_claim_ids, "current_phase": "phase4_claims"}


async def _load_claim_context(db, claim_ids: list[str], generation_run_id: str) -> tuple[list[Claim], dict[str, dict]]:
    claim_uuid_ids = [uuid.UUID(cid) for cid in claim_ids]
    claim_result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuid_ids)))
    claims = list(claim_result.scalars().all())
    claims.sort(key=lambda claim: (claim.created_at, claim.id))

    context: dict[str, dict] = {
        str(claim.id): {
            "claim_type": claim.claim_type,
            "normalized_statement": normalize_research_text(claim.statement).lower(),
            "normalized_attribution": _normalize_attribution(claim.attribution),
            "value_tokens": _value_tokens(claim.statement),
            "numbers_and_dates": _numbers_and_dates(claim.statement),
            "evidence_ids": set(),
            "source_ids": set(),
            "branch_ids": set(),
            "question_ids": set(),
        }
        for claim in claims
    }

    evidence_result = await db.execute(
        select(ClaimEvidence, EvidenceSpan, Source)
        .join(EvidenceSpan, ClaimEvidence.evidence_id == EvidenceSpan.id)
        .join(Source, EvidenceSpan.source_id == Source.id)
        .where(ClaimEvidence.claim_id.in_(claim_uuid_ids))
    )
    evidence_ids: set[uuid.UUID] = set()
    evidence_to_claims: dict[uuid.UUID, set[str]] = defaultdict(set)
    for claim_evidence, evidence, source in evidence_result.all():
        claim_id = str(claim_evidence.claim_id)
        if claim_id not in context:
            continue
        context[claim_id]["evidence_ids"].add(str(evidence.id))
        context[claim_id]["source_ids"].add(str(source.is_duplicate_of or source.id))
        evidence_ids.add(evidence.id)
        evidence_to_claims[evidence.id].add(claim_id)

    if evidence_ids:
        provenance_result = await db.execute(
            select(EvidenceProvenance).where(
                EvidenceProvenance.evidence_id.in_(list(evidence_ids)),
                EvidenceProvenance.generation_run_id == uuid.UUID(generation_run_id),
            )
        )
        for provenance in provenance_result.scalars():
            for claim_id in evidence_to_claims.get(provenance.evidence_id, set()):
                context[claim_id]["branch_ids"].add(provenance.branch_id)
                context[claim_id]["question_ids"].add(provenance.question_id)

    return claims, context


def _normalize_attribution(attribution: dict | None) -> str:
    if not isinstance(attribution, dict):
        return ""
    normalized = {
        key: normalize_research_text(str(value)).lower()
        for key, value in sorted(attribution.items())
        if value
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _value_tokens(statement: str) -> set[str]:
    normalized = normalize_research_text(statement).lower()
    tokens = {
        token
        for token in re.findall(r"[\w$.-]+", normalized)
        if len(token) > 2 and token not in _STOPWORDS
    }
    return tokens


def _numbers_and_dates(statement: str) -> set[str]:
    normalized = normalize_research_text(statement).lower()
    return set(re.findall(r"\b\d[\d,.:/-]*\b", normalized))


def _token_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


def _claims_match(left: dict, right: dict) -> bool:
    if left["claim_type"] != right["claim_type"]:
        return False
    if left["normalized_attribution"] != right["normalized_attribution"]:
        return False
    if left["normalized_statement"] == right["normalized_statement"]:
        return True
    if left["numbers_and_dates"] != right["numbers_and_dates"]:
        return False
    if not (
        left["evidence_ids"] & right["evidence_ids"]
        or left["source_ids"] & right["source_ids"]
        or left["branch_ids"] & right["branch_ids"]
    ):
        return False
    return _token_overlap(left["value_tokens"], right["value_tokens"]) >= 0.88


async def _merge_claims(db, *, keep_claim_id: uuid.UUID, drop_claim_id: uuid.UUID) -> None:
    keep_result = await db.execute(select(ClaimEvidence).where(ClaimEvidence.claim_id == keep_claim_id))
    keep_links = {row.evidence_id: row for row in keep_result.scalars().all()}

    drop_result = await db.execute(select(ClaimEvidence).where(ClaimEvidence.claim_id == drop_claim_id))
    for link in drop_result.scalars().all():
        current = keep_links.get(link.evidence_id)
        if current is None:
            db.add(
                ClaimEvidence(
                    claim_id=keep_claim_id,
                    evidence_id=link.evidence_id,
                    supports_fact=link.supports_fact,
                    contradicts_fact=link.contradicts_fact,
                    illustrative=link.illustrative,
                )
            )
            continue
        merged_supports = current.supports_fact or link.supports_fact
        merged_contradicts = current.contradicts_fact or link.contradicts_fact
        if merged_supports and merged_contradicts:
            logger.warning(
                "Skipping contradictory claim-evidence merge for claim %s evidence %s",
                keep_claim_id,
                link.evidence_id,
            )
            continue
        current.supports_fact = merged_supports
        current.contradicts_fact = merged_contradicts
        current.illustrative = current.illustrative or link.illustrative
    await db.flush()
