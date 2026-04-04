"""Phase 4.2: Verifier — hybrid rule-based + LLM claim verification."""

import hashlib
import json
import logging
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.claim import Claim, ClaimEvidence, VerificationLevel
from app.models.generation_run import GenerationRun
from app.models.episode_manifest import EpisodeManifest
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.source import Source
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.research_context import build_question_context_map
from app.pipeline.state import PipelineState
from app.realtime.events import claim_verified, progress, step_completed, step_started
from app.realtime.publisher import publish_event, update_generation_progress
from app.services.llm import call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

VERIFIER_SYSTEM = """You are a claim verification engine for a podcast pipeline.
Given a batch of claims with their supporting evidence, evaluate whether the evidence truly supports each claim.

Return a JSON array:
[{
  "claim_id": "uuid",
  "verification_level": "VERIFIED|OBSERVED|UNVERIFIED|DISPUTED",
  "reasoning": "brief explanation",
  "needs_research": false
}]

Rules:
- VERIFIED: evidence clearly and directly confirms the claim from 2+ independent canonical sources, or from 1 strong primary/high source with direct textual support
- OBSERVED: evidence supports but not conclusively (single source, temporary source, non-primary source, or partial semantic match)
- UNVERIFIED: evidence doesn't adequately support the claim
- DISPUTED: evidence contains contradictory information
- needs_research=true: claim is important but current evidence is insufficient — request more research

Reasoning rules:
- Keep reasoning to one short evidence-bound explanation.
- Do not speculate about the current date, whether a source date is in the future, hidden metadata anomalies, or anything not explicitly present in the payload.
- If evidence is partial or truncated, say that directly instead of inferring the missing part.
- If a source is weak, explain it using only provided fields such as single-source, temporary, authority_type, or source_reliability.
- The payload may include a representative subset of support/provenance when there are many items; use summary counts and omitted-item counters rather than assuming the list is exhaustive.

Evaluate semantic alignment, not just keyword overlap. Respect source metadata:
- temporary-only evidence cannot justify VERIFIED
- source authority is a signal, not an automatic proof
- prefer direct evidence for factual confirmation; contextual evidence can strengthen framing but may not fully verify the statement
- for REPORT/OPINION, check that attribution and quote alignment are meaningful."""

VERIFY_BATCH_SIZE = 8
VERIFY_BATCH_CHAR_BUDGET = 18000
VERIFIER_MAX_EVIDENCE_ITEMS = 12
VERIFIER_MAX_PROVENANCE_PAIRS = 12
VERIFIER_MAX_BRANCH_IDS = 16
VERIFIER_MAX_QUESTION_IDS = 24
VERIFIER_MAX_INTENTS = 8
VERIFIER_MIN_QUOTE_EXCERPT_CHARS = 240
VERIFIER_HARD_QUOTE_FLOOR_CHARS = 32
VERIFIER_QUOTE_OMISSION_MARKER = "\n...[excerpt clipped for verifier budget]...\n"
_ATTRIBUTION_PLACEHOLDERS = {
    "unknown",
    "unspecified expert",
    "unidentified expert",
    "anonymous source",
    "source",
    "observer",
    "неустановленный эксперт",
    "источник",
    "наблюдатель",
    "анонимный источник",
}


def _index_provenance_asset_types(provenances: list[EvidenceProvenance]) -> dict[uuid.UUID, list[str]]:
    asset_types_by_evidence: dict[uuid.UUID, set[str]] = {}
    for provenance in provenances:
        asset_types_by_evidence.setdefault(provenance.evidence_id, set()).add(str(provenance.asset_type).upper())
    return {
        evidence_id: sorted(asset_types)
        for evidence_id, asset_types in asset_types_by_evidence.items()
    }


def _index_provenance_contexts(
    provenances: list[EvidenceProvenance],
    question_context_map: dict[str, dict] | None = None,
) -> tuple[dict[uuid.UUID, list[dict]], list[dict]]:
    question_context_map = question_context_map or {}
    contexts_by_evidence: dict[uuid.UUID, list[dict]] = defaultdict(list)
    pair_summaries: dict[tuple[str, str], dict] = {}
    for provenance in provenances:
        question_context = question_context_map.get(provenance.question_id, {})
        context = {
            "branch_id": provenance.branch_id,
            "question_id": provenance.question_id,
            "question": question_context.get("question", ""),
            "intent": question_context.get("intent", ""),
            "direct_or_contextual": provenance.direct_or_contextual,
        }
        contexts_by_evidence[provenance.evidence_id].append(context)

        pair_key = (provenance.branch_id, provenance.question_id)
        summary = pair_summaries.setdefault(
            pair_key,
            {
                "branch_id": provenance.branch_id,
                "question_id": provenance.question_id,
                "question": question_context.get("question", ""),
                "intent": question_context.get("intent", ""),
                "found_assets": set(),
                "direct_count": 0,
                "contextual_count": 0,
            },
        )
        summary["found_assets"].add(str(provenance.asset_type).upper())
        if provenance.direct_or_contextual == "direct":
            summary["direct_count"] += 1
        elif provenance.direct_or_contextual == "contextual":
            summary["contextual_count"] += 1

    normalized_contexts = {
        evidence_id: sorted(
            {
                json.dumps(item, ensure_ascii=False, sort_keys=True): item
                for item in items
            }.values(),
            key=lambda item: (item.get("branch_id", ""), item.get("question_id", "")),
        )
        for evidence_id, items in contexts_by_evidence.items()
    }
    normalized_pairs = []
    for summary in pair_summaries.values():
        normalized_pairs.append(
            {
                **summary,
                "found_assets": sorted(summary["found_assets"]),
            }
        )
    normalized_pairs.sort(key=lambda item: (item["branch_id"], item["question_id"]))
    return normalized_contexts, normalized_pairs


async def _load_claim_evidence(
    claim: Claim,
    db: AsyncSession,
    *,
    generation_run_id: str | None = None,
    question_context_map: dict[str, dict] | None = None,
) -> dict:
    """Load supporting/counter evidence for a claim."""
    ce_result = await db.execute(
        select(ClaimEvidence, EvidenceSpan, Source)
        .join(EvidenceSpan, ClaimEvidence.evidence_id == EvidenceSpan.id)
        .join(Source, EvidenceSpan.source_id == Source.id)
        .where(ClaimEvidence.claim_id == claim.id)
    )
    rows = ce_result.all()

    supporting: list[dict] = []
    has_counter = False
    source_ids: set[str] = set()
    supporting_evidence_ids: list[uuid.UUID] = []

    for ce, ev, src in rows:
        if ce.contradicts_fact:
            has_counter = True
        if not ce.supports_fact:
            continue

        canonical_source_id = str(src.is_duplicate_of or src.id)
        source_ids.add(canonical_source_id)
        supporting_evidence_ids.append(ev.id)
        supporting.append(
            {
                "evidence_id": str(ev.id),
                "quote": ev.verbatim_quote,
                "asset_type": ev.asset_type,
                "asset_types": [str(ev.asset_type).upper()],
                "canonical_asset_type": ev.asset_type,
                "source_title": src.title or "",
                "source_url": src.original_url or "",
                "source_type": src.source_type,
                "published_at": src.published_at.isoformat() if src.published_at else None,
                "source_reliability": src.domain_reliability,
                "authority_type": src.authority_type,
                "is_temporary": src.is_temporary,
                "source_id": str(src.id),
                "canonical_source_id": canonical_source_id,
            }
        )

    provenance_pairs: list[dict] = []
    found_assets: set[str] = set()
    if supporting_evidence_ids and generation_run_id:
        provenance_result = await db.execute(
            select(EvidenceProvenance).where(
                EvidenceProvenance.evidence_id.in_(supporting_evidence_ids),
                EvidenceProvenance.generation_run_id == uuid.UUID(generation_run_id),
            )
        )
        provenances = list(provenance_result.scalars())
        asset_types_by_evidence = _index_provenance_asset_types(provenances)
        evidence_question_contexts, provenance_pairs = _index_provenance_contexts(
            provenances,
            question_context_map=question_context_map,
        )
        for item in supporting:
            evidence_id = uuid.UUID(item["evidence_id"])
            contextual_asset_types = asset_types_by_evidence.get(
                evidence_id,
                [str(item["canonical_asset_type"]).upper()],
            )
            question_contexts = evidence_question_contexts.get(evidence_id, [])
            item["asset_types"] = contextual_asset_types
            item["asset_type"] = contextual_asset_types[0].lower()
            item["question_contexts"] = question_contexts
            item["question_intents"] = sorted(
                {
                    str(context.get("intent", "")).strip()
                    for context in question_contexts
                    if str(context.get("intent", "")).strip()
                }
            )
            item["direct_or_contextual"] = sorted(
                {
                    str(context.get("direct_or_contextual", "")).strip()
                    for context in question_contexts
                    if str(context.get("direct_or_contextual", "")).strip()
                }
            )
            found_assets.update(contextual_asset_types)
    else:
        for item in supporting:
            found_assets.update(item["asset_types"])
            item["question_contexts"] = []
            item["question_intents"] = []
            item["direct_or_contextual"] = []

    return {
        "supporting": supporting,
        "has_counter": has_counter,
        "source_ids": source_ids,
        "all_supporting_temporary": bool(supporting) and all(item["is_temporary"] for item in supporting),
        "branch_ids": sorted({pair["branch_id"] for pair in provenance_pairs}),
        "question_ids": sorted({pair["question_id"] for pair in provenance_pairs}),
        "intents": sorted({pair["intent"] for pair in provenance_pairs if pair.get("intent")}),
        "provenance_pairs": provenance_pairs,
        "found_assets": sorted(found_assets),
        "direct_evidence_count": sum(1 for item in supporting if "direct" in item.get("direct_or_contextual", [])),
        "contextual_evidence_count": sum(1 for item in supporting if "contextual" in item.get("direct_or_contextual", [])),
    }


async def verify_single_claim(
    claim: Claim,
    db: AsyncSession,
    *,
    generation_run_id: str | None = None,
    question_context_map: dict[str, dict] | None = None,
) -> VerificationLevel:
    """Verify a single claim using the hybrid approach."""
    evidence_data = await _load_claim_evidence(
        claim,
        db,
        generation_run_id=generation_run_id,
        question_context_map=question_context_map,
    )
    prefiltered = _prefilter_claim(claim, evidence_data)
    if prefiltered is not None:
        return prefiltered

    payload = _build_verifier_claim_payload(claim, evidence_data)
    result, _, _ = await _llm_verify_batch(
        [payload]
    )
    if result:
        level = _parse_verification_level(result[0].get("verification_level", "UNVERIFIED"))
        return _apply_verification_ceiling(level, evidence_data)

    return _rule_based_fallback(evidence_data)


def _prefilter_claim(claim: Claim, evidence_data: dict) -> VerificationLevel | None:
    if evidence_data["has_counter"]:
        return VerificationLevel.DISPUTED
    if not evidence_data["supporting"]:
        return VerificationLevel.UNVERIFIED
    if claim.claim_type in ("opinion", "report"):
        return VerificationLevel.OBSERVED if _is_meaningful_attribution(claim.attribution) else VerificationLevel.UNVERIFIED
    return None


async def _llm_verify_batch(claims_payload: list[dict]) -> tuple[list[dict], str, object]:
    prompt = json.dumps({"claims": claims_payload}, ensure_ascii=False)
    raw = ""
    parsed: object = None
    try:
        raw = await call_sonnet_structured(
            system=VERIFIER_SYSTEM,
            prompt=prompt,
            max_tokens=4096,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        if isinstance(parsed, list):
            return parsed, raw, parsed
        if isinstance(parsed, dict) and "results" in parsed:
            return parsed["results"], raw, parsed
        return ([parsed] if isinstance(parsed, dict) else []), raw, parsed
    except Exception:
        logger.warning("LLM verification failed, falling back to rule-based", exc_info=True)
        return [], raw, parsed


def _parse_verification_level(level_str: str) -> VerificationLevel:
    mapping = {
        "VERIFIED": VerificationLevel.VERIFIED,
        "OBSERVED": VerificationLevel.OBSERVED,
        "UNVERIFIED": VerificationLevel.UNVERIFIED,
        "DISPUTED": VerificationLevel.DISPUTED,
    }
    return mapping.get(level_str.upper(), VerificationLevel.UNVERIFIED)


def _has_strong_primary_support(supporting: list[dict]) -> bool:
    return any(
        item["authority_type"] == "primary"
        and item["source_reliability"] == "high"
        and not item["is_temporary"]
        for item in supporting
    )


def _apply_verification_ceiling(level: VerificationLevel, evidence_data: dict) -> VerificationLevel:
    if level != VerificationLevel.VERIFIED:
        return level
    if evidence_data["all_supporting_temporary"]:
        return VerificationLevel.OBSERVED
    if len(evidence_data["source_ids"]) >= 2:
        return level
    if _has_strong_primary_support(evidence_data["supporting"]):
        return level
    return VerificationLevel.OBSERVED


def _rule_based_fallback(evidence_data: dict) -> VerificationLevel:
    if evidence_data["has_counter"]:
        return VerificationLevel.DISPUTED
    if not evidence_data["supporting"]:
        return VerificationLevel.UNVERIFIED
    if evidence_data["all_supporting_temporary"]:
        return VerificationLevel.OBSERVED
    if len(evidence_data["source_ids"]) >= 2:
        return VerificationLevel.VERIFIED
    if _has_strong_primary_support(evidence_data["supporting"]):
        return VerificationLevel.OBSERVED
    if len(evidence_data["source_ids"]) == 1:
        return VerificationLevel.OBSERVED
    return VerificationLevel.UNVERIFIED


def _is_meaningful_attribution(attribution: dict | None) -> bool:
    if not isinstance(attribution, dict):
        return False
    who = attribution.get("who")
    if not isinstance(who, str):
        return False
    normalized = who.strip().lower()
    if not normalized or normalized in _ATTRIBUTION_PLACEHOLDERS:
        return False
    return len(normalized) >= 3


def _normalize_required_assets(required_assets: list[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    for item in required_assets or []:
        if not isinstance(item, dict):
            continue
        asset_type = str(item.get("type", "")).strip().upper()
        if not asset_type:
            continue
        try:
            min_count = int(item.get("min_count", 1) or 1)
        except Exception:
            min_count = 1
        normalized.append({"type": asset_type, "min_count": max(min_count, 1)})
    return normalized


def _support_strength(evidence_data: dict) -> str:
    direct_count = int(evidence_data.get("direct_evidence_count", 0) or 0)
    contextual_count = int(evidence_data.get("contextual_evidence_count", 0) or 0)
    source_count = len(evidence_data.get("source_ids", set()) or set())
    supporting = evidence_data.get("supporting", []) or []
    authority_types = {str(item.get("authority_type", "")).lower() for item in supporting}

    if direct_count == 0 and contextual_count > 0:
        return "contextual_only"
    if source_count <= 1 and authority_types and authority_types <= {"tertiary", "unknown"}:
        return "weak_authority"
    if source_count <= 1:
        return "single_source"
    return "needs_independent_confirmation"


def _existing_support_summary(evidence_data: dict) -> str:
    support_strength = _support_strength(evidence_data)
    source_count = len(evidence_data.get("source_ids", set()) or set())
    supporting = evidence_data.get("supporting", []) or []
    authority_types = sorted(
        {
            str(item.get("authority_type", "")).lower()
            for item in supporting
            if str(item.get("authority_type", "")).strip()
        }
    )
    direct_count = int(evidence_data.get("direct_evidence_count", 0) or 0)
    contextual_count = int(evidence_data.get("contextual_evidence_count", 0) or 0)
    authority_label = ", ".join(authority_types) if authority_types else "unknown-authority"
    return (
        f"{support_strength}; {source_count} source(s); "
        f"{direct_count} direct / {contextual_count} contextual; "
        f"authority={authority_label}"
    )


def _search_objective_for_gap(claim: Claim, evidence_data: dict) -> str:
    strength = _support_strength(evidence_data)
    if strength == "contextual_only":
        return f"Find a direct source that explicitly confirms: {claim.statement}"
    if strength == "weak_authority":
        return f"Find a more authoritative direct source that confirms: {claim.statement}"
    if strength == "single_source":
        return f"Find an independent second source that confirms: {claim.statement}"
    return f"Find independent corroboration for: {claim.statement}"


def _build_gap_claim_context(claim: Claim, evidence_data: dict) -> dict:
    return {
        "claim_id": str(claim.id),
        "claim_statement": claim.statement,
        "support_strength": _support_strength(evidence_data),
        "search_objective": _search_objective_for_gap(claim, evidence_data),
        "existing_support_summary": _existing_support_summary(evidence_data),
    }


def _merge_gap_claim_contexts(existing_contexts: list[dict], new_context: dict) -> list[dict]:
    merged_by_claim_id: dict[str, dict] = {
        str(context.get("claim_id", "")).strip(): context
        for context in existing_contexts
        if isinstance(context, dict) and str(context.get("claim_id", "")).strip()
    }
    claim_id = str(new_context.get("claim_id", "")).strip()
    if claim_id:
        merged_by_claim_id[claim_id] = new_context
    return [
        merged_by_claim_id[key]
        for key in sorted(merged_by_claim_id)
    ]


def _summarize_gap_claim_contexts(claim_contexts: list[dict]) -> tuple[str, str, str, str, list[str]]:
    statements = []
    objectives = []
    strengths = []
    support_summaries = []
    claim_ids = []
    for context in claim_contexts:
        if not isinstance(context, dict):
            continue
        claim_id = str(context.get("claim_id", "")).strip()
        if claim_id:
            claim_ids.append(claim_id)
        statement = str(context.get("claim_statement", "")).strip()
        if statement:
            statements.append(statement)
        objective = str(context.get("search_objective", "")).strip()
        if objective:
            objectives.append(objective)
        strength = str(context.get("support_strength", "")).strip()
        if strength:
            strengths.append(strength)
        summary = str(context.get("existing_support_summary", "")).strip()
        if summary:
            support_summaries.append(summary)

    def _dedupe_keep_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    ordered_statements = _dedupe_keep_order(statements)
    ordered_objectives = _dedupe_keep_order(objectives)
    ordered_strengths = _dedupe_keep_order(strengths)
    ordered_support_summaries = _dedupe_keep_order(support_summaries)

    summary_statement = " | ".join(ordered_statements[:3])
    summary_objective = " | ".join(ordered_objectives[:3])
    summary_strength = ", ".join(ordered_strengths)
    summary_support = " || ".join(ordered_support_summaries[:3])
    return summary_statement, summary_objective, summary_strength, summary_support, _dedupe_keep_order(claim_ids)


def _merge_question_contexts(existing_contexts: list[dict], new_context: dict) -> list[dict]:
    keyed: dict[tuple[str, str], dict] = {}
    for context in existing_contexts:
        if not isinstance(context, dict):
            continue
        key = (
            str(context.get("branch_id", "")).strip(),
            str(context.get("question_id", "")).strip(),
        )
        if not any(key):
            continue
        keyed[key] = {
            **context,
            "required_assets": _normalize_required_assets(context.get("required_assets", [])),
            "expected_assets": sorted(
                str(asset).upper()
                for asset in (context.get("expected_assets", []) or [])
                if isinstance(asset, str) and asset
            ),
            "found_assets": sorted(
                str(asset).upper()
                for asset in (context.get("found_assets", []) or [])
                if isinstance(asset, str) and asset
            ),
            "missing_assets": sorted(
                str(asset).upper()
                for asset in (context.get("missing_assets", []) or [])
                if isinstance(asset, str) and asset
            ),
        }

    key = (
        str(new_context.get("branch_id", "")).strip(),
        str(new_context.get("question_id", "")).strip(),
    )
    if not any(key):
        return sorted(keyed.values(), key=lambda item: (item.get("branch_id", ""), item.get("question_id", "")))

    normalized_new = {
        **new_context,
        "required_assets": _normalize_required_assets(new_context.get("required_assets", [])),
        "expected_assets": sorted(
            str(asset).upper()
            for asset in (new_context.get("expected_assets", []) or [])
            if isinstance(asset, str) and asset
        ),
        "found_assets": sorted(
            str(asset).upper()
            for asset in (new_context.get("found_assets", []) or [])
            if isinstance(asset, str) and asset
        ),
        "missing_assets": sorted(
            str(asset).upper()
            for asset in (new_context.get("missing_assets", []) or [])
            if isinstance(asset, str) and asset
        ),
    }
    current = keyed.get(key)
    if current is None:
        keyed[key] = normalized_new
    else:
        merged_required: dict[str, int] = {
            asset["type"]: int(asset.get("min_count", 1) or 1)
            for asset in current.get("required_assets", [])
        }
        for asset in normalized_new.get("required_assets", []):
            asset_type = asset["type"]
            merged_required[asset_type] = max(merged_required.get(asset_type, 0), int(asset.get("min_count", 1) or 1))
        merged_found = sorted(set(current.get("found_assets", [])) | set(normalized_new.get("found_assets", [])))
        keyed[key] = {
            "branch_id": key[0],
            "question_id": key[1],
            "question": normalized_new.get("question") or current.get("question") or "",
            "intent": normalized_new.get("intent") or current.get("intent") or "",
            "required_assets": [
                {"type": asset_type, "min_count": min_count}
                for asset_type, min_count in sorted(merged_required.items())
            ],
            "expected_assets": sorted(set(current.get("expected_assets", [])) | set(normalized_new.get("expected_assets", []))),
            "found_assets": merged_found,
            "missing_assets": sorted(
                asset_type
                for asset_type, min_count in merged_required.items()
                if merged_found.count(asset_type) < 1 and asset_type not in merged_found
            ),
        }

    return sorted(keyed.values(), key=lambda item: (item.get("branch_id", ""), item.get("question_id", "")))


def _merge_overlapping_research_gaps(gaps: list[dict]) -> list[dict]:
    remaining = [gap for gap in gaps if isinstance(gap, dict)]
    merged_groups: list[dict] = []

    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        claim_ids = {str(claim_id).strip() for claim_id in (seed.get("claim_ids", []) or []) if str(claim_id).strip()}
        expanded = True
        while expanded:
            expanded = False
            next_remaining: list[dict] = []
            for candidate in remaining:
                candidate_claim_ids = {
                    str(claim_id).strip()
                    for claim_id in (candidate.get("claim_ids", []) or [])
                    if str(claim_id).strip()
                }
                if claim_ids and candidate_claim_ids and claim_ids & candidate_claim_ids:
                    group.append(candidate)
                    claim_ids.update(candidate_claim_ids)
                    expanded = True
                else:
                    next_remaining.append(candidate)
            remaining = next_remaining

        if len(group) == 1:
            merged_groups.append(seed)
            continue

        merged_claim_contexts: list[dict] = []
        merged_question_contexts: list[dict] = []
        merged_required: dict[str, int] = {}
        merged_expected: set[str] = set()
        merged_found: set[str] = set()
        priorities: list[int] = []

        for gap in group:
            priorities.append(int(gap.get("priority", 1) or 1))
            for context in gap.get("claim_contexts", []) or []:
                if isinstance(context, dict):
                    merged_claim_contexts = _merge_gap_claim_contexts(merged_claim_contexts, context)
            for question_context in gap.get("question_contexts", []) or []:
                if isinstance(question_context, dict):
                    merged_question_contexts = _merge_question_contexts(merged_question_contexts, question_context)
            for asset in gap.get("required_assets", []) or []:
                if not isinstance(asset, dict):
                    continue
                asset_type = str(asset.get("type", "")).strip().upper()
                if not asset_type:
                    continue
                merged_required[asset_type] = max(merged_required.get(asset_type, 0), int(asset.get("min_count", 1) or 1))
            merged_expected.update(
                str(asset).upper()
                for asset in (gap.get("expected_assets", []) or [])
                if isinstance(asset, str) and asset
            )
            merged_found.update(
                str(asset).upper()
                for asset in (gap.get("found_assets", []) or [])
                if isinstance(asset, str) and asset
            )

        lead_question_context = (
            sorted(merged_question_contexts, key=lambda item: (item.get("branch_id", ""), item.get("question_id", "")))[0]
            if merged_question_contexts else {}
        )
        statement, objective, strength, support_summary, merged_claim_ids = _summarize_gap_claim_contexts(merged_claim_contexts)
        merged_groups.append(
            {
                "question_id": str(lead_question_context.get("question_id", "")).strip(),
                "branch_id": str(lead_question_context.get("branch_id", "")).strip(),
                "question": lead_question_context.get("question") or statement,
                "intent": lead_question_context.get("intent", ""),
                "priority": min(priorities) if priorities else 1,
                "missing_assets": sorted(asset_type for asset_type in merged_required if asset_type not in merged_found),
                "required_assets": [
                    {"type": asset_type, "min_count": min_count}
                    for asset_type, min_count in sorted(merged_required.items())
                ],
                "expected_assets": sorted(merged_expected),
                "found_assets": sorted(merged_found),
                "gap_origin": "verifier",
                "claim_contexts": merged_claim_contexts,
                "claim_statement": statement,
                "search_objective": objective,
                "support_strength": strength,
                "existing_support_summary": support_summary,
                "claim_ids": merged_claim_ids,
                "claim_id": merged_claim_ids[0] if merged_claim_ids else "",
                "question_contexts": merged_question_contexts,
            }
        )

    return sorted(merged_groups, key=lambda gap: (gap.get("branch_id", ""), gap.get("question_id", "")))


def _build_research_gaps(
    needs_research_claims: list[tuple[Claim, dict]],
    *,
    question_context_map: dict[str, dict] | None = None,
) -> list[dict]:
    question_context_map = question_context_map or {}
    gaps: dict[tuple[str, str], dict] = {}
    for claim, evidence_data in needs_research_claims:
        pair_contexts = evidence_data.get("provenance_pairs") or [
            {
                "branch_id": "verify",
                "question_id": f"verify:{claim.id}",
                "found_assets": evidence_data.get("found_assets", []),
            }
        ]
        for pair in pair_contexts:
            branch_id = pair["branch_id"]
            question_id = pair["question_id"]
            key = (branch_id, question_id)
            question_context = question_context_map.get(question_id, {})
            required_assets = _normalize_required_assets(question_context.get("required_evidence", []))
            expected_assets = sorted(
                str(asset).upper()
                for asset in (question_context.get("expected_evidence", []) or [])
                if isinstance(asset, str) and asset
            )
            found_assets = list(pair.get("found_assets", evidence_data.get("found_assets", [])))
            missing_assets = sorted(
                asset["type"]
                for asset in required_assets
                if asset["type"] not in found_assets
            )
            claim_context = _build_gap_claim_context(claim, evidence_data)
            if key not in gaps:
                gaps[key] = {
                    "question_id": question_id,
                    "branch_id": branch_id,
                    "question": pair.get("question") or question_context.get("question") or claim.statement,
                    "intent": pair.get("intent", "") or question_context.get("intent", ""),
                    "priority": 1,
                    "missing_assets": missing_assets,
                    "required_assets": required_assets,
                    "expected_assets": expected_assets,
                    "found_assets": found_assets,
                    "gap_origin": "verifier",
                    "claim_contexts": [claim_context],
                    "question_contexts": [
                        {
                            "question_id": question_id,
                            "branch_id": branch_id,
                            "question": pair.get("question") or question_context.get("question") or claim.statement,
                            "intent": pair.get("intent", "") or question_context.get("intent", ""),
                            "required_assets": required_assets,
                            "expected_assets": expected_assets,
                            "found_assets": found_assets,
                            "missing_assets": missing_assets,
                        }
                    ],
                }
                (
                    gaps[key]["claim_statement"],
                    gaps[key]["search_objective"],
                    gaps[key]["support_strength"],
                    gaps[key]["existing_support_summary"],
                    claim_ids,
                ) = _summarize_gap_claim_contexts(gaps[key]["claim_contexts"])
                gaps[key]["claim_ids"] = claim_ids
                gaps[key]["claim_id"] = claim_ids[0] if claim_ids else ""
                continue
            merged_assets = set(gaps[key].get("found_assets", []))
            merged_assets.update(found_assets)
            gaps[key]["found_assets"] = sorted(merged_assets)
            gaps[key]["missing_assets"] = sorted(
                asset["type"]
                for asset in gaps[key].get("required_assets", [])
                if asset["type"] not in gaps[key]["found_assets"]
            )
            gaps[key]["claim_contexts"] = _merge_gap_claim_contexts(
                gaps[key].get("claim_contexts", []),
                claim_context,
            )
            gaps[key]["question_contexts"] = _merge_question_contexts(
                gaps[key].get("question_contexts", []),
                {
                    "question_id": question_id,
                    "branch_id": branch_id,
                    "question": pair.get("question") or question_context.get("question") or claim.statement,
                    "intent": pair.get("intent", "") or question_context.get("intent", ""),
                    "required_assets": required_assets,
                    "expected_assets": expected_assets,
                    "found_assets": gaps[key]["found_assets"],
                    "missing_assets": gaps[key]["missing_assets"],
                },
            )
            (
                gaps[key]["claim_statement"],
                gaps[key]["search_objective"],
                gaps[key]["support_strength"],
                gaps[key]["existing_support_summary"],
                claim_ids,
            ) = _summarize_gap_claim_contexts(gaps[key]["claim_contexts"])
            gaps[key]["claim_ids"] = claim_ids
            gaps[key]["claim_id"] = claim_ids[0] if claim_ids else ""
    return _merge_overlapping_research_gaps(list(gaps.values()))


def _compute_verification_fingerprint(claim: Claim, evidence_data: dict) -> str:
    supporting = []
    for item in evidence_data.get("supporting", []):
        direct_or_contextual = item.get("direct_or_contextual", [])
        supporting.append(
            {
                "evidence_id": str(item.get("evidence_id", "")),
                "quote": str(item.get("quote", "")),
                "asset_types": sorted(str(asset).upper() for asset in (item.get("asset_types", []) or [])),
                "canonical_source_id": str(item.get("canonical_source_id", "")),
                "source_reliability": str(item.get("source_reliability", "")),
                "authority_type": str(item.get("authority_type", "")),
                "is_temporary": bool(item.get("is_temporary", False)),
                "direct_or_contextual": sorted(str(value) for value in direct_or_contextual if str(value).strip()),
            }
        )
    supporting.sort(key=lambda item: (item["evidence_id"], item["canonical_source_id"]))

    provenance_pairs = [
        {
            "branch_id": str(item.get("branch_id", "")),
            "question_id": str(item.get("question_id", "")),
            "intent": str(item.get("intent", "")),
            "found_assets": sorted(str(asset).upper() for asset in (item.get("found_assets", []) or [])),
            "direct_count": int(item.get("direct_count", 0) or 0),
            "contextual_count": int(item.get("contextual_count", 0) or 0),
        }
        for item in evidence_data.get("provenance_pairs", [])
    ]
    provenance_pairs.sort(key=lambda item: (item["branch_id"], item["question_id"]))

    fingerprint_payload = {
        "statement": claim.statement,
        "claim_type": claim.claim_type,
        "has_counter": bool(evidence_data.get("has_counter", False)),
        "supporting": supporting,
        "branch_ids": sorted(str(item) for item in (evidence_data.get("branch_ids", []) or [])),
        "question_ids": sorted(str(item) for item in (evidence_data.get("question_ids", []) or [])),
        "intents": sorted(str(item) for item in (evidence_data.get("intents", []) or [])),
        "provenance_pairs": provenance_pairs,
    }
    encoded = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _derive_verify_scope(state: PipelineState, claim_ids: list[str]) -> list[str]:
    ordered_claim_ids = list(dict.fromkeys(claim_ids))
    iteration_counts = state.get("iteration_counts", {}) or {}
    if iteration_counts.get("verify_research_loops", 0) < 1:
        return ordered_claim_ids

    scope_ids = {
        str(claim_id).strip()
        for claim_id in (state.get("rebuilt_claim_ids", []) or [])
        if str(claim_id).strip()
    }
    scope_ids.update(
        str(claim_id).strip()
        for claim_id in (state.get("dedupe_merged_into_claim_ids", []) or [])
        if str(claim_id).strip()
    )
    for gap in (state.get("research_gaps", []) or []):
        if not isinstance(gap, dict):
            continue
        gap_claim_ids = [
            str(claim_id).strip()
            for claim_id in (gap.get("claim_ids", []) or [])
            if str(claim_id).strip()
        ]
        if gap_claim_ids:
            scope_ids.update(gap_claim_ids)
            continue
        claim_id = str(gap.get("claim_id", "")).strip()
        if claim_id:
            scope_ids.add(claim_id)
    if not scope_ids:
        return ordered_claim_ids
    return [claim_id for claim_id in ordered_claim_ids if claim_id in scope_ids]


def _should_force_full_verify_for_config(config: dict | None, *, node_name: str) -> bool:
    """Return True when a step-mode rerun explicitly targets this node."""
    if not isinstance(config, dict):
        return False
    step_control = config.get("step_control")
    if not isinstance(step_control, dict):
        return False
    requested_action = step_control.get("requested_action")
    if requested_action != "rerun":
        return False
    requested_target_node = step_control.get("requested_target_node")
    if isinstance(requested_target_node, str) and requested_target_node.strip():
        return requested_target_node.strip() == node_name
    last_node = step_control.get("last_node")
    return isinstance(last_node, str) and last_node == node_name


async def _should_force_full_verify_for_node_rerun(
    db,
    *,
    generation_run_id: str,
    node_name: str,
) -> bool:
    try:
        run_uuid = uuid.UUID(generation_run_id)
    except (TypeError, ValueError):
        return False
    result = await db.execute(select(GenerationRun.config).where(GenerationRun.id == run_uuid))
    return _should_force_full_verify_for_config(result.scalar_one_or_none(), node_name=node_name)


def _build_verifier_claim_payload(claim: Claim, evidence_data: dict) -> dict:
    payload = {
        "claim_id": str(claim.id),
        "statement": claim.statement[:500],
        "evidence": [_compact_verifier_evidence_item(item) for item in evidence_data["supporting"]],
        "independent_source_count": len(evidence_data["source_ids"]),
        "branch_ids": evidence_data["branch_ids"],
        "question_ids": evidence_data["question_ids"],
        "intents": evidence_data["intents"],
        "direct_evidence_count": evidence_data["direct_evidence_count"],
        "contextual_evidence_count": evidence_data["contextual_evidence_count"],
        "provenance_pairs": [_compact_verifier_provenance_pair(item) for item in evidence_data["provenance_pairs"]],
    }
    return _fit_verifier_payload_to_budget(payload)


def _estimate_verifier_payload_chars(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) + 64


def _clip_verifier_quote(quote: str, max_chars: int) -> str:
    if len(quote) <= max_chars:
        return quote
    if max_chars <= len(VERIFIER_QUOTE_OMISSION_MARKER) + 80:
        return quote[:max_chars]
    available = max_chars - len(VERIFIER_QUOTE_OMISSION_MARKER)
    head_chars = max(60, int(available * 0.6))
    tail_chars = max(20, available - head_chars)
    if head_chars + tail_chars > available:
        tail_chars = max(20, available - head_chars)
    if head_chars + tail_chars >= len(quote):
        return quote
    head = quote[:head_chars].rstrip()
    tail = quote[-tail_chars:].lstrip()
    return f"{head}{VERIFIER_QUOTE_OMISSION_MARKER}{tail}"


def _compact_verifier_evidence_item(item: dict) -> dict:
    direct_or_contextual = item.get("direct_or_contextual", [])
    if isinstance(direct_or_contextual, list):
        normalized_directness = sorted({str(value).strip() for value in direct_or_contextual if str(value).strip()})
    else:
        normalized_directness = []
    asset_types = item.get("asset_types", [])
    return {
        "quote": str(item.get("quote", "")),
        "asset_type": str(item.get("asset_type", "")),
        "asset_types": list(asset_types) if isinstance(asset_types, list) else [],
        "source_reliability": item.get("source_reliability"),
        "authority_type": item.get("authority_type"),
        "is_temporary": bool(item.get("is_temporary", False)),
        "canonical_source_id": str(item.get("canonical_source_id", "")),
        "direct_or_contextual": normalized_directness,
    }


def _compact_verifier_provenance_pair(item: dict) -> dict:
    found_assets = item.get("found_assets", [])
    return {
        "branch_id": str(item.get("branch_id", "")),
        "question_id": str(item.get("question_id", "")),
        "intent": str(item.get("intent", "")),
        "found_assets": list(found_assets) if isinstance(found_assets, list) else [],
        "direct_count": int(item.get("direct_count", 0) or 0),
        "contextual_count": int(item.get("contextual_count", 0) or 0),
    }


def _verifier_evidence_rank(item: dict) -> tuple[int, int, int, int, str]:
    directness = item.get("direct_or_contextual", [])
    source_reliability = str(item.get("source_reliability", "")).lower()
    authority_type = str(item.get("authority_type", "")).lower()
    reliability_rank = {"high": 0, "medium": 1, "low": 2, "unknown": 3}.get(source_reliability, 4)
    authority_rank = {"primary": 0, "secondary": 1, "tertiary": 2, "unknown": 3}.get(authority_type, 4)
    direct_rank = 0 if "direct" in directness else 1
    temporary_rank = 1 if item.get("is_temporary") else 0
    return (
        direct_rank,
        temporary_rank,
        reliability_rank,
        authority_rank,
        str(item.get("canonical_source_id", "")),
    )


def _normalize_verifier_list_budget(
    payload: dict,
    *,
    evidence_limit: int,
    provenance_limit: int,
) -> dict:
    fitted_payload = dict(payload)
    evidence = [dict(item) for item in payload.get("evidence", []) if isinstance(item, dict)]
    provenance_pairs = [dict(item) for item in payload.get("provenance_pairs", []) if isinstance(item, dict)]

    total_evidence = len(evidence)
    total_provenance = len(provenance_pairs)

    if total_evidence > evidence_limit:
        evidence = sorted(evidence, key=_verifier_evidence_rank)[:evidence_limit]
        fitted_payload["omitted_supporting_evidence_count"] = total_evidence - len(evidence)
    else:
        fitted_payload.pop("omitted_supporting_evidence_count", None)

    if total_provenance > provenance_limit:
        provenance_pairs = provenance_pairs[:provenance_limit]
        fitted_payload["omitted_provenance_pair_count"] = total_provenance - len(provenance_pairs)
    else:
        fitted_payload.pop("omitted_provenance_pair_count", None)

    fitted_payload["supporting_evidence_count"] = total_evidence
    fitted_payload["provenance_pair_count"] = total_provenance
    fitted_payload["evidence"] = evidence
    fitted_payload["provenance_pairs"] = provenance_pairs
    return fitted_payload


def _normalize_verifier_top_level_context(payload: dict) -> dict:
    fitted_payload = dict(payload)

    branch_ids = [str(item) for item in payload.get("branch_ids", [])]
    question_ids = [str(item) for item in payload.get("question_ids", [])]
    intents = [str(item) for item in payload.get("intents", [])]

    if len(branch_ids) > VERIFIER_MAX_BRANCH_IDS:
        fitted_payload["branch_ids"] = branch_ids[:VERIFIER_MAX_BRANCH_IDS]
        fitted_payload["omitted_branch_id_count"] = len(branch_ids) - VERIFIER_MAX_BRANCH_IDS
    else:
        fitted_payload["branch_ids"] = branch_ids
        fitted_payload.pop("omitted_branch_id_count", None)

    if len(question_ids) > VERIFIER_MAX_QUESTION_IDS:
        fitted_payload["question_ids"] = question_ids[:VERIFIER_MAX_QUESTION_IDS]
        fitted_payload["omitted_question_id_count"] = len(question_ids) - VERIFIER_MAX_QUESTION_IDS
    else:
        fitted_payload["question_ids"] = question_ids
        fitted_payload.pop("omitted_question_id_count", None)

    if len(intents) > VERIFIER_MAX_INTENTS:
        fitted_payload["intents"] = intents[:VERIFIER_MAX_INTENTS]
        fitted_payload["omitted_intent_count"] = len(intents) - VERIFIER_MAX_INTENTS
    else:
        fitted_payload["intents"] = intents
        fitted_payload.pop("omitted_intent_count", None)

    return fitted_payload


def _apply_quote_budget(
    payload: dict,
    *,
    budget: int,
    min_excerpt_chars: int,
    hard_floor_chars: int,
) -> dict:
    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list) or not evidence:
        return payload

    normalized_evidence = [dict(item) for item in evidence if isinstance(item, dict)]
    if not normalized_evidence:
        return payload

    original_quotes = [str(item.get("quote", "")) for item in normalized_evidence]
    blank_quote_payload = {
        **payload,
        "evidence": [{**item, "quote": ""} for item in normalized_evidence],
    }
    non_quote_chars = _estimate_verifier_payload_chars(blank_quote_payload)
    available_for_quotes = budget - non_quote_chars
    total_quote_chars = sum(len(quote) for quote in original_quotes)

    if total_quote_chars <= 0:
        return {**payload, "evidence": normalized_evidence}

    min_total = min_excerpt_chars * len(original_quotes)
    if available_for_quotes >= min_total:
        remaining_budget = available_for_quotes
        remaining_quote_chars = total_quote_chars
        allocated: list[int] = []
        remaining_slots = len(original_quotes)
        for quote in original_quotes:
            remaining_slots -= 1
            if remaining_quote_chars <= 0:
                target = min_excerpt_chars
            else:
                proportional = int(remaining_budget * len(quote) / remaining_quote_chars)
                min_reserved_for_rest = min_excerpt_chars * remaining_slots
                max_for_current = max(min_excerpt_chars, remaining_budget - min_reserved_for_rest)
                target = max(min_excerpt_chars, min(proportional, max_for_current))
            target = min(target, len(quote))
            allocated.append(target)
            remaining_budget -= target
            remaining_quote_chars -= len(quote)
    else:
        even_target = max(hard_floor_chars, available_for_quotes // len(original_quotes)) if available_for_quotes > 0 else hard_floor_chars
        allocated = [min(len(quote), even_target) for quote in original_quotes]

    for item, quote, target_chars in zip(normalized_evidence, original_quotes, allocated, strict=False):
        item["quote"] = _clip_verifier_quote(quote, target_chars)

    fitted_payload = {**payload, "evidence": normalized_evidence}

    while _estimate_verifier_payload_chars(fitted_payload) > budget:
        longest_index = max(
            range(len(normalized_evidence)),
            key=lambda idx: len(str(normalized_evidence[idx].get("quote", ""))),
            default=-1,
        )
        if longest_index < 0:
            break
        current_quote = str(normalized_evidence[longest_index].get("quote", ""))
        if len(current_quote) <= hard_floor_chars:
            break
        overshoot = _estimate_verifier_payload_chars(fitted_payload) - budget
        next_target = max(hard_floor_chars, len(current_quote) - max(overshoot + 32, 120))
        normalized_evidence[longest_index]["quote"] = _clip_verifier_quote(original_quotes[longest_index], next_target)

    return fitted_payload


def _fit_verifier_payload_to_budget(payload: dict, budget: int = VERIFY_BATCH_CHAR_BUDGET) -> dict:
    if _estimate_verifier_payload_chars(payload) <= budget:
        return payload

    fitted_payload = _normalize_verifier_top_level_context(payload)
    if _estimate_verifier_payload_chars(fitted_payload) <= budget:
        return fitted_payload

    fitted_payload = _normalize_verifier_list_budget(
        fitted_payload,
        evidence_limit=VERIFIER_MAX_EVIDENCE_ITEMS,
        provenance_limit=VERIFIER_MAX_PROVENANCE_PAIRS,
    )
    fitted_payload = _apply_quote_budget(
        fitted_payload,
        budget=budget,
        min_excerpt_chars=VERIFIER_MIN_QUOTE_EXCERPT_CHARS,
        hard_floor_chars=VERIFIER_HARD_QUOTE_FLOOR_CHARS,
    )
    if _estimate_verifier_payload_chars(fitted_payload) <= budget:
        return fitted_payload

    compact_provenance_pairs = [
        {
            "branch_id": item.get("branch_id", ""),
            "question_id": item.get("question_id", ""),
            "intent": item.get("intent", ""),
        }
        for item in fitted_payload.get("provenance_pairs", [])
        if isinstance(item, dict)
    ]
    fitted_payload = {
        **fitted_payload,
        "provenance_pairs": compact_provenance_pairs,
    }
    fitted_payload = _apply_quote_budget(
        fitted_payload,
        budget=budget,
        min_excerpt_chars=120,
        hard_floor_chars=VERIFIER_HARD_QUOTE_FLOOR_CHARS,
    )
    if _estimate_verifier_payload_chars(fitted_payload) <= budget:
        return fitted_payload

    while len(fitted_payload.get("evidence", [])) > 1 and _estimate_verifier_payload_chars(fitted_payload) > budget:
        evidence = list(fitted_payload.get("evidence", []))
        evidence.pop()
        fitted_payload["evidence"] = evidence
        fitted_payload["omitted_supporting_evidence_count"] = max(
            int(fitted_payload.get("supporting_evidence_count", len(evidence))) - len(evidence),
            0,
        )
        fitted_payload = _apply_quote_budget(
            fitted_payload,
            budget=budget,
            min_excerpt_chars=80,
            hard_floor_chars=VERIFIER_HARD_QUOTE_FLOOR_CHARS,
        )
    if _estimate_verifier_payload_chars(fitted_payload) <= budget:
        return fitted_payload

    if fitted_payload.get("provenance_pairs"):
        fitted_payload["omitted_provenance_pair_count"] = int(fitted_payload.get("provenance_pair_count", 0))
        fitted_payload["provenance_pairs"] = []
        fitted_payload = _apply_quote_budget(
            fitted_payload,
            budget=budget,
            min_excerpt_chars=80,
            hard_floor_chars=VERIFIER_HARD_QUOTE_FLOOR_CHARS,
        )

    return fitted_payload


def _split_verifier_batches(claims_for_llm: list[tuple[Claim, dict]]) -> list[list[tuple[Claim, dict, dict]]]:
    batches: list[list[tuple[Claim, dict, dict]]] = []
    current_batch: list[tuple[Claim, dict, dict]] = []
    current_chars = 0
    for claim, evidence_data in claims_for_llm:
        payload = _build_verifier_claim_payload(claim, evidence_data)
        payload_chars = _estimate_verifier_payload_chars(payload)
        if current_batch and (
            len(current_batch) >= VERIFY_BATCH_SIZE
            or current_chars + payload_chars > VERIFY_BATCH_CHAR_BUDGET
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append((claim, evidence_data, payload))
        current_chars += payload_chars
    if current_batch:
        batches.append(current_batch)
    return batches


async def verify_claims(state: PipelineState) -> dict:
    """LangGraph node: verify claims using hybrid rule-based + LLM approach."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase4_claims", "verify", "Verifying claims"))
    update_generation_progress(run_id, "phase4_claims", "verifier", 43.0)

    claim_ids = state.get("claim_ids", [])
    if not claim_ids:
        return {
            "claim_ids": claim_ids,
            "rebuilt_claim_ids": [],
            "preserved_claim_ids": [],
            "targeted_branch_ids": [],
            "dedupe_deleted_claim_ids": [],
            "dedupe_merged_into_claim_ids": [],
            "dedupe_untouched_claim_ids": [],
        }

    iteration_counts = dict(state.get("iteration_counts", {}) or {})
    needs_research_claims: list[tuple[Claim, dict]] = []
    prefiltered_debug_results: list[dict] = []
    llm_batch_debug_results: list[dict] = []
    previous_gap_claim_ids: set[str] = set()
    for gap in (state.get("research_gaps", []) or []):
        if not isinstance(gap, dict):
            continue
        gap_claim_ids = [
            str(claim_id).strip()
            for claim_id in (gap.get("claim_ids", []) or [])
            if str(claim_id).strip()
        ]
        if gap_claim_ids:
            previous_gap_claim_ids.update(gap_claim_ids)
            continue
        claim_id = str(gap.get("claim_id", "")).strip()
        if claim_id:
            previous_gap_claim_ids.add(claim_id)
    processed_claim_ids: list[str] = []
    skipped_unchanged_claim_ids: list[str] = []
    force_full_reverify = False

    async with async_session_factory() as db:
        force_full_reverify = await _should_force_full_verify_for_node_rerun(
            db,
            generation_run_id=run_id,
            node_name="verify_claims",
        )
        scope_state = state
        if force_full_reverify:
            iteration_counts["verify_research_loops"] = 0
            iteration_counts["verify_needs_research"] = False
            scope_state = {
                **state,
                "iteration_counts": dict(iteration_counts),
            }
        verify_scope_claim_ids = set(_derive_verify_scope(scope_state, claim_ids))

        question_context_map: dict[str, dict] = {}
        if state.get("manifest_id"):
            manifest_result = await db.execute(
                select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
            )
            manifest = manifest_result.scalar_one_or_none()
            question_context_map = build_question_context_map(
                manifest.research_tree_json if manifest else None
            )
        claims_for_llm: list[tuple[Claim, dict]] = []

        for cid in claim_ids:
            if verify_scope_claim_ids and cid not in verify_scope_claim_ids:
                continue
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            claim = result.scalar_one_or_none()
            if not claim:
                continue

            evidence_data = await _load_claim_evidence(
                claim,
                db,
                generation_run_id=run_id,
                question_context_map=question_context_map,
            )
            fingerprint = _compute_verification_fingerprint(claim, evidence_data)
            processed_claim_ids.append(cid)

            if (
                not force_full_reverify
                and claim.verification_fingerprint
                and claim.verification_fingerprint == fingerprint
            ):
                skipped_unchanged_claim_ids.append(cid)
                if cid in previous_gap_claim_ids and claim.claim_type == "fact":
                    needs_research_claims.append((claim, evidence_data))
                continue

            prefiltered = _prefilter_claim(claim, evidence_data)
            if prefiltered is not None:
                claim.verification_level = prefiltered
                claim.verification_fingerprint = fingerprint
                if claim.claim_type == "fact" and prefiltered == VerificationLevel.UNVERIFIED:
                    needs_research_claims.append((claim, evidence_data))
                prefiltered_debug_results.append(
                    {
                        "claim_id": cid,
                        "claim_type": claim.claim_type,
                        "statement": claim.statement[:500],
                        "verification_level": prefiltered,
                        "evidence": evidence_data["supporting"],
                        "branch_ids": evidence_data["branch_ids"],
                        "question_ids": evidence_data["question_ids"],
                        "intents": evidence_data["intents"],
                    }
                )
                publish_event(run_id, claim_verified(cid, prefiltered))
                continue

            claim.verification_fingerprint = fingerprint
            claims_for_llm.append((claim, evidence_data))

        verifier_batches = _split_verifier_batches(claims_for_llm)
        for batch_number, batch in enumerate(verifier_batches, start=1):
            batch_payload = [payload for _, _, payload in batch]

            llm_results, llm_raw, llm_parsed = await _llm_verify_batch(batch_payload)
            result_map = {
                r["claim_id"]: r
                for r in llm_results
                if isinstance(r, dict) and "claim_id" in r
            }
            batch_final_results: list[dict] = []

            for claim, ev_data, _ in batch:
                cid = str(claim.id)
                llm_result = result_map.get(cid)

                if llm_result:
                    pre_ceiling_level = _parse_verification_level(llm_result.get("verification_level", "UNVERIFIED"))
                    level = _apply_verification_ceiling(pre_ceiling_level, ev_data)
                    if llm_result.get("needs_research") or level == VerificationLevel.UNVERIFIED:
                        needs_research_claims.append((claim, ev_data))
                else:
                    pre_ceiling_level = None
                    level = _rule_based_fallback(ev_data)
                    if level == VerificationLevel.UNVERIFIED:
                        needs_research_claims.append((claim, ev_data))

                claim.verification_level = level
                batch_final_results.append(
                    {
                        "claim_id": cid,
                        "claim_type": claim.claim_type,
                        "statement": claim.statement[:500],
                        "llm_result": llm_result,
                        "pre_ceiling_level": pre_ceiling_level,
                        "final_level": level,
                        "needs_research": bool((llm_result or {}).get("needs_research")) or level == VerificationLevel.UNVERIFIED,
                        "branch_ids": ev_data["branch_ids"],
                        "question_ids": ev_data["question_ids"],
                        "intents": ev_data["intents"],
                        "direct_evidence_count": ev_data["direct_evidence_count"],
                        "contextual_evidence_count": ev_data["contextual_evidence_count"],
                    }
                )
                publish_event(run_id, claim_verified(cid, level))

            llm_batch_debug_results.append(
                {
                    "batch_number": batch_number,
                    "status": "ok" if llm_results else "llm_fallback",
                    "input": {"claims": batch_payload},
                    "llm_output": {
                        "raw_chars": len(llm_raw or ""),
                        "raw": llm_raw,
                        "parsed_type": type(llm_parsed).__name__ if llm_parsed is not None else None,
                        "parsed": llm_parsed,
                    },
                    "final_results": batch_final_results,
                }
            )

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "verify"))
    publish_event(run_id, progress(45.0, f"Verified {len(processed_claim_ids) or len(claim_ids)} claims"))

    verify_loops = iteration_counts.get("verify_research_loops", 0)
    needs_research = bool(needs_research_claims) and verify_loops < settings.max_verifier_research_loops
    if needs_research:
        iteration_counts["verify_research_loops"] = verify_loops + 1
    iteration_counts["verify_needs_research"] = needs_research
    research_gaps = _build_research_gaps(
        needs_research_claims,
        question_context_map=question_context_map if 'question_context_map' in locals() else {},
    ) if needs_research else []

    if prefiltered_debug_results:
        _dump_verifier_debug(
            run_id=run_id,
            dump_type="prefiltered",
            payload={
                "run_id": run_id,
                "prefiltered_claim_count": len(prefiltered_debug_results),
                "claims": prefiltered_debug_results,
            },
        )
    for batch_debug in llm_batch_debug_results:
        _dump_verifier_debug(
            run_id=run_id,
            dump_type=f"batch-{batch_debug['batch_number']:03d}",
            payload={"run_id": run_id, **batch_debug},
        )
    _dump_verifier_debug(
        run_id=run_id,
        dump_type="summary",
        payload={
            "run_id": run_id,
            "claim_count": len(claim_ids),
            "verify_scope_claim_count": len(verify_scope_claim_ids) if verify_scope_claim_ids else len(claim_ids),
            "processed_claim_count": len(processed_claim_ids),
            "skipped_unchanged_claim_count": len(skipped_unchanged_claim_ids),
            "prefiltered_claim_count": len(prefiltered_debug_results),
            "llm_batch_count": len(llm_batch_debug_results),
            "needs_research": needs_research,
            "research_gap_count": len(research_gaps),
            "research_gaps": research_gaps,
            "iteration_counts": iteration_counts,
        },
    )

    return {
        "claim_ids": claim_ids,
        "current_phase": "phase4_claims",
        "iteration_counts": iteration_counts,
        "research_gaps": research_gaps,
        "rebuilt_claim_ids": [],
        "preserved_claim_ids": [],
        "targeted_branch_ids": [],
        "dedupe_deleted_claim_ids": [],
        "dedupe_merged_into_claim_ids": [],
        "dedupe_untouched_claim_ids": [],
    }


def _dump_verifier_debug(*, run_id: str, dump_type: str, payload: dict) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="verifier",
        filename_stem=f"run-{safe_filename_token(run_id)}_{safe_filename_token(dump_type)}",
        payload=payload,
    )
