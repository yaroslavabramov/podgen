"""Phase 4.15: deterministic global claim dedupe before verification."""

import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import bindparam, select, text, update

from app.models.claim import Claim, ClaimEvidence
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.generation_run import GenerationRun
from app.models.source import Source
from app.config import settings
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.phase3_research.normalization import normalize_research_text
from app.pipeline.phase4_claims.claim_notes import decode_claim_notes_metadata
from app.pipeline.phase4_claims.claim_type_utils import VALID_CLAIM_TYPES, normalize_claim_type
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.step_control import StepExecutionStopped
from app.pipeline.state import PipelineState
from app.realtime.events import progress, step_completed, step_started
from app.realtime.publisher import publish_event, update_generation_progress
from app.services.llm import call_haiku_structured
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
    requested_claim_ids = list(dict.fromkeys(state.get("claim_ids", [])))
    if not requested_claim_ids:
        return {
            "claim_ids": requested_claim_ids,
            "dedupe_deleted_claim_ids": [],
            "dedupe_merged_into_claim_ids": [],
            "dedupe_untouched_claim_ids": [],
            "current_phase": "phase4_claims",
        }

    publish_event(run_id, step_started("phase4_claims", "dedupe_claims", "Collapsing duplicate claims"))
    update_generation_progress(run_id, "phase4_claims", "claim_dedupe", 41.0)

    async with async_session_factory() as db:
        execution_context = await _load_dedupe_execution_context(db, run_id)
        claims, contexts, pool_stats = await _load_claim_context(db, requested_claim_ids, run_id)
        duplicate_map: dict[str, str] = {}
        merged_into_claim_ids: set[str] = set()
        ordered_ids = [str(claim.id) for claim in claims]
        rebuilt_claim_ids = set(str(cid) for cid in state.get("rebuilt_claim_ids", []) or [])

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
                    merged_into_claim_ids.add(claim_id)

        if duplicate_map:
            for dropped_id in duplicate_map:
                dropped = next((claim for claim in claims if str(claim.id) == dropped_id), None)
                if dropped is not None:
                    await db.delete(dropped)
            await db.commit()

        lexical_dropped = len(duplicate_map)

        # --- Semantic dedup pass (Haiku) ---
        surviving_claims = [c for c in claims if str(c.id) not in duplicate_map]
        _semantic_branch_log: list[dict] = []
        semantic_drops = await _semantic_dedupe_pass(
            db, surviving_claims, contexts, duplicate_map, merged_into_claim_ids,
            branch_log=_semantic_branch_log,
        )
        if semantic_drops:
            for dropped_id in semantic_drops:
                dropped = next((c for c in claims if str(c.id) == dropped_id), None)
                if dropped is not None:
                    await db.delete(dropped)
            await db.commit()
            logger.info(
                "Semantic dedup: dropped %d claims (lexical: %d)",
                len(semantic_drops), lexical_dropped,
            )

        kept_claim_ids = [cid for cid in ordered_ids if cid not in duplicate_map]
        untouched_claim_ids = [
            cid for cid in kept_claim_ids
            if cid not in rebuilt_claim_ids and cid not in merged_into_claim_ids
        ]

        _dump_dedupe_debug(
            run_id=run_id,
            execution_context=execution_context,
            requested_claim_id_count=pool_stats["requested_claim_id_count"],
            loaded_claim_row_count=pool_stats["loaded_claim_row_count"],
            missing_claim_id_count=pool_stats["missing_claim_id_count"],
            missing_claim_ids=pool_stats["missing_claim_ids"],
            lexical_dropped=lexical_dropped,
            semantic_dropped=len(semantic_drops),
            kept_count=len(kept_claim_ids),
            duplicate_map=duplicate_map,
            claims=claims,
            contexts=contexts,
            semantic_branch_details=_semantic_branch_log,
            rebuilt_claim_ids=rebuilt_claim_ids,
        )

    publish_event(run_id, step_completed("phase4_claims", "dedupe_claims"))
    publish_event(
        run_id,
        progress(42.0, f"Collapsed {pool_stats['loaded_claim_row_count'] - len(kept_claim_ids)} duplicate claims"),
    )
    return {
        "claim_ids": kept_claim_ids,
        "dedupe_deleted_claim_ids": sorted(duplicate_map.keys()),
        "dedupe_merged_into_claim_ids": [cid for cid in kept_claim_ids if cid in merged_into_claim_ids],
        "dedupe_untouched_claim_ids": untouched_claim_ids,
        "current_phase": "phase4_claims",
    }


async def _load_claim_context(
    db,
    claim_ids: list[str],
    generation_run_id: str,
) -> tuple[list[Claim], dict[str, dict], dict[str, object]]:
    requested_claim_ids = list(dict.fromkeys(claim_ids))
    claim_uuid_ids: list[uuid.UUID] = []
    valid_request_ids: list[str] = []
    invalid_request_ids: list[str] = []
    for claim_id in requested_claim_ids:
        try:
            claim_uuid_ids.append(uuid.UUID(claim_id))
            valid_request_ids.append(claim_id)
        except (TypeError, ValueError):
            invalid_request_ids.append(str(claim_id))
    if not claim_uuid_ids:
        return [], {}, {
            "requested_claim_id_count": len(requested_claim_ids),
            "loaded_claim_row_count": 0,
            "missing_claim_id_count": len(invalid_request_ids),
            "missing_claim_ids": invalid_request_ids,
        }
    await _repair_invalid_claim_types(db, claim_uuid_ids)
    claim_result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuid_ids)))
    claims = list(claim_result.scalars().all())
    claims.sort(key=lambda claim: (claim.created_at, claim.id))
    loaded_claim_ids = {str(claim.id) for claim in claims}

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

    missing_claim_ids = invalid_request_ids + [
        claim_id for claim_id in valid_request_ids
        if claim_id not in loaded_claim_ids
    ]

    return claims, context, {
        "requested_claim_id_count": len(requested_claim_ids),
        "loaded_claim_row_count": len(claims),
        "missing_claim_id_count": len(missing_claim_ids),
        "missing_claim_ids": missing_claim_ids,
    }


async def _load_dedupe_execution_context(db, generation_run_id: str) -> dict[str, object]:
    default = {
        "input_origin": "pipeline_state_claim_ids",
        "is_step_rerun": False,
        "requested_action": None,
        "requested_target_node": None,
        "last_node": None,
    }
    try:
        run_uuid = uuid.UUID(generation_run_id)
    except (TypeError, ValueError):
        return default

    result = await db.execute(select(GenerationRun.config).where(GenerationRun.id == run_uuid))
    config = result.scalar_one_or_none()
    if not isinstance(config, dict):
        return default
    step_control = config.get("step_control")
    if not isinstance(step_control, dict):
        return default

    requested_action = step_control.get("requested_action")
    requested_target_node = step_control.get("requested_target_node")
    last_node = step_control.get("last_node")
    is_step_rerun = requested_action == "rerun"
    input_origin = "pipeline_state_claim_ids"
    if is_step_rerun and (
        requested_target_node == "dedupe_claims"
        or (
            not isinstance(requested_target_node, str)
            and isinstance(last_node, str)
            and last_node == "dedupe_claims"
        )
    ):
        input_origin = "checkpoint_pre_node_state"

    return {
        "input_origin": input_origin,
        "is_step_rerun": bool(is_step_rerun),
        "requested_action": requested_action if isinstance(requested_action, str) else None,
        "requested_target_node": requested_target_node if isinstance(requested_target_node, str) else None,
        "last_node": last_node if isinstance(last_node, str) else None,
    }


async def _repair_invalid_claim_types(db, claim_ids: list[uuid.UUID]) -> None:
    if not claim_ids:
        return

    select_invalid = text(
        """
        SELECT id, claim_type, attribution
        FROM claims
        WHERE id IN :claim_ids
          AND lower(claim_type) NOT IN ('fact', 'opinion', 'report')
        """
    ).bindparams(bindparam("claim_ids", expanding=True))
    invalid_rows = (await db.execute(select_invalid, {"claim_ids": claim_ids})).mappings().all()
    if not invalid_rows:
        return

    for row in invalid_rows:
        normalized_claim_type, _ = normalize_claim_type(row["claim_type"], row.get("attribution"))
        if normalized_claim_type not in VALID_CLAIM_TYPES:
            normalized_claim_type = "fact"
        await db.execute(
            update(Claim.__table__)
            .where(Claim.__table__.c.id == row["id"])
            .values(claim_type=normalized_claim_type)
        )
    await db.flush()


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


MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE = 3
MAX_CLAIMS_PER_HAIKU_CALL = 40
SEMANTIC_BATCH_STRIDE = 20


async def _semantic_dedupe_pass(
    db,
    surviving_claims: list,
    contexts: dict[str, dict],
    duplicate_map: dict[str, str],
    merged_into_claim_ids: set[str],
    branch_log: list[dict] | None = None,
) -> list[str]:
    """Group claims by builder branch, ask Haiku to find semantic duplicates, then do a cross-branch pass."""
    branch_groups: dict[str, list] = defaultdict(list)
    for claim in surviving_claims:
        meta = decode_claim_notes_metadata(claim.notes)
        branch = meta.get("builder_branch_id", "_unknown")
        branch_groups[branch].append(claim)

    # --- Per-branch pass: Haiku calls in parallel, merges sequential ---
    haiku_tasks = []
    branch_ids_in_order: list[str] = []
    for branch_id, branch_claims in branch_groups.items():
        if len(branch_claims) < MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE:
            if branch_log is not None:
                branch_log.append({
                    "branch_id": branch_id,
                    "claim_count": len(branch_claims),
                    "skipped": True,
                    "reason": f"below minimum ({MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE})",
                })
            continue
        for batch in _build_semantic_batches(branch_claims, contexts):
            branch_ids_in_order.append(branch_id)
            haiku_tasks.append(_semantic_dedupe_haiku(
                branch_id, batch, contexts, duplicate_map,
            ))

    all_dropped: list[str] = []
    if haiku_tasks:
        haiku_results = await asyncio.gather(*haiku_tasks, return_exceptions=True)
        for i, result in enumerate(haiku_results):
            bid = branch_ids_in_order[i] if i < len(branch_ids_in_order) else "?"
            if isinstance(result, StepExecutionStopped):
                raise result
            if isinstance(result, Exception):
                logger.warning("Semantic dedup branch %s failed: %s", bid, result)
                if branch_log is not None:
                    branch_log.append({"branch_id": bid, "error": str(result)})
                continue
            suggested_merges = [
                {
                    "keep_id": keep_id,
                    "drop_id": drop_id,
                    "reason": reason,
                    "keep_statement": str(contexts.get(keep_id, {}).get("normalized_statement", ""))[:300],
                    "drop_statement": str(contexts.get(drop_id, {}).get("normalized_statement", ""))[:300],
                }
                for keep_id, drop_id, reason in result
            ]
            applied_merges: list[dict[str, str]] = []
            # Apply merges sequentially (safe for single db session)
            for keep_id, drop_id, reason in result:
                logger.info("Semantic dedup (branch %s): merging %s into %s — %s",
                            bid, drop_id[:12], keep_id[:12], reason)
                await _merge_claims(db, keep_claim_id=uuid.UUID(keep_id), drop_claim_id=uuid.UUID(drop_id))
                duplicate_map[drop_id] = keep_id
                merged_into_claim_ids.add(keep_id)
                all_dropped.append(drop_id)
                applied_merges.append({
                    "keep_id": keep_id,
                    "drop_id": drop_id,
                    "reason": reason,
                })
            if branch_log is not None:
                dropped_ids = [d for _, d, _ in result]
                branch_log.append({
                    "branch_id": bid,
                    "claim_count": len(branch_groups.get(bid, [])),
                    "suggested_merge_count": len(suggested_merges),
                    "suggested_merges": suggested_merges,
                    "applied_merge_count": len(applied_merges),
                    "applied_merges": applied_merges,
                    "dropped_count": len(dropped_ids),
                    "dropped_ids": dropped_ids,
                })

    # --- Cross-branch pass: dedupe across all branches (batched) ---
    cross_surviving = [c for c in surviving_claims if str(c.id) not in duplicate_map]
    if len(cross_surviving) >= MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE:
        cross_dropped: list[str] = []
        batches = _build_semantic_batches(cross_surviving, contexts)
        cross_suggested_merges: list[dict[str, str | int | list[int]]] = []
        cross_consolidated_merges: list[dict[str, str | int | list[int] | bool]] = []
        cross_applied_merges: list[dict[str, str | int]] = []
        cross_skipped_merges: list[dict[str, str | int]] = []

        if batches:
            cross_haiku_tasks = [
                _semantic_dedupe_haiku(
                    f"_cross_branch_{j}", batch, contexts, duplicate_map,
                )
                for j, batch in enumerate(batches)
            ]
            cross_haiku_results = await asyncio.gather(*cross_haiku_tasks, return_exceptions=True)
            for batch_index, result in enumerate(cross_haiku_results):
                if isinstance(result, StepExecutionStopped):
                    raise result
                if isinstance(result, Exception):
                    logger.warning("Semantic dedup cross-branch batch failed: %s", result)
                    continue
                for keep_id, drop_id, reason in result:
                    cross_suggested_merges.append({
                        "batch_index": batch_index,
                        "keep_id": keep_id,
                        "drop_id": drop_id,
                        "reason": reason,
                        "keep_statement": str(contexts.get(keep_id, {}).get("normalized_statement", ""))[:300],
                        "drop_statement": str(contexts.get(drop_id, {}).get("normalized_statement", ""))[:300],
                    })
            for merge in _consolidate_cross_branch_suggestions(cross_suggested_merges, contexts):
                cross_consolidated_merges.append(dict(merge))
                resolved_keep_id = _resolve_live_claim_id(str(merge["keep_id"]), duplicate_map)
                resolved_drop_id = _resolve_live_claim_id(str(merge["drop_id"]), duplicate_map)
                if resolved_keep_id == resolved_drop_id:
                    cross_skipped_merges.append({
                        "keep_id": str(merge["keep_id"]),
                        "drop_id": str(merge["drop_id"]),
                        "resolved_keep_id": resolved_keep_id,
                        "resolved_drop_id": resolved_drop_id,
                        "reason": str(merge["reason"]),
                        "skip_reason": "same_live_claim_after_resolution",
                    })
                    continue
                logger.info(
                    "Semantic dedup (cross-branch): merging %s into %s — %s",
                    resolved_drop_id[:12],
                    resolved_keep_id[:12],
                    merge["reason"],
                )
                await _merge_claims(
                    db,
                    keep_claim_id=uuid.UUID(resolved_keep_id),
                    drop_claim_id=uuid.UUID(resolved_drop_id),
                )
                duplicate_map[resolved_drop_id] = resolved_keep_id
                merged_into_claim_ids.add(resolved_keep_id)
                all_dropped.append(resolved_drop_id)
                cross_dropped.append(resolved_drop_id)
                cross_applied_merges.append({
                    "keep_id": str(merge["keep_id"]),
                    "drop_id": str(merge["drop_id"]),
                    "resolved_keep_id": resolved_keep_id,
                    "resolved_drop_id": resolved_drop_id,
                    "reason": str(merge["reason"]),
                })

        if branch_log is not None:
            branch_log.append({
                "branch_id": "_cross_branch",
                "claim_count": len(cross_surviving),
                "batch_count": len(batches),
                "suggested_merge_count": len(cross_suggested_merges),
                "suggested_merges": cross_suggested_merges,
                "consolidated_merge_count": len(cross_consolidated_merges),
                "consolidated_merges": cross_consolidated_merges,
                "applied_merge_count": len(cross_applied_merges),
                "applied_merges": cross_applied_merges,
                "skipped_merge_count": len(cross_skipped_merges),
                "skipped_merges": cross_skipped_merges,
                "dropped_count": len(cross_dropped),
                "dropped_ids": cross_dropped,
            })

    return all_dropped


_DEDUPE_SYSTEM = """\
You are a strict claim deduplication assistant for a podcast research pipeline.
Your job is to find TRUE duplicate claims.
A duplicate means two claims that are genuinely interchangeable for downstream verification, planning, and writing.
Return JSON only.

## Core rule
Merge claims ONLY when they express the same proposition and a script could keep either one without losing any distinct historical step, legal step, causal step, implementation step, actor/action distinction, or comparison move.

If keeping both claims would preserve a meaningful distinction for downstream narrative structure, they are NOT duplicates.

When in doubt, keep both.

## Mutual-substitutability test
Two claims are duplicates only if ALL of the following are true:
1. They assert the same proposition, not merely the same topic.
2. Neither claim adds a distinct stage in a sequence.
3. Neither claim adds a distinct actor, action, decision, legal act, implementation step, consequence, or comparison.
4. One claim is not simply a broader statement that contains the other as one component.
5. A writer or planner would never need both claims as separate beats.

If any of these fail, do NOT merge.

## What counts as a duplicate
These usually ARE duplicates:
- near-identical restatements of the same fact
- paraphrases of the same legal or institutional rule
- the same proposition expressed with different wording, order, or light detail level
- translations of the same claim
- one shorter and one slightly fuller version of the same proposition, but only if the fuller version does not add a distinct step or move

## What is NOT a duplicate
These are NOT duplicates, even if they are about the same topic, institution, reform, episode, or period:
- proposal / recommendation / plan vs official adoption / enactment / implementation
- problem / precondition / setup vs response / solution / institutional answer
- de facto action / occupation / seizure / announcement vs later treaty / ratification / legal formalization
- rule / framework / definition vs operational mechanism / implementation step
- mechanism / constraint vs consequence / downstream effect / later adjustment
- initial measure vs later rejection / rollback / closure / restoration
- earlier regime / state vs later regime shift
- invalidation / withdrawal of one instrument vs restoration / recognition of another
- baseline fact vs comparison / contrast / "unusual because..." framing
- general definition vs specific local implementation
- fact vs opinion / evaluation / endorsement / criticism
- any pair where one claim could reasonably become its own planning beat

## Invalid reasons for merge
These are NOT valid merge rationales:
- "same topic"
- "same event"
- "same episode"
- "same reform"
- "same historical moment"
- "same core proposition" when the claims describe different steps within that episode
- "one is more complete"
- "one contains the other"
- "the broader claim includes the narrower fact"
- "they belong together narratively"

If your reason depends on any of these, do NOT merge.

## Contrastive examples

NOT duplicates:
- "A reform was proposed" vs "the reform was officially adopted"
- "A territory was occupied" vs "a later treaty formalized control"
- "A rule required collateral" vs "issuers had to deposit collateral before issuing units"
- "The old notes were invalidated" vs "the earlier currency was restored as legal tender"
- "A system requires full backing" vs "the system automatically adjusts when demand changes"
- "A baseline fact" vs "the same fact used inside a comparison with other countries"

Duplicates:
- "A law granted legal-tender status to notes above a threshold amount" vs "A statute made notes above that threshold legal tender"
- "Three licensed firms issue the notes" vs "The notes are issued by three authorized firms under license"
- "The monetary base is fully backed by reserve assets" vs "Reserve assets fully cover the monetary base"

## Keep selection
For each true duplicate group, keep the single best claim:
- prefer the clearest wording
- prefer the claim with stronger evidence_count
- prefer the more complete wording ONLY if it does not add a distinct move that should survive separately

Do not merge chains loosely.
Do not use a broad summary claim to absorb distinct narrower claims unless they are truly interchangeable.

## Output format
Return ONLY groups with 2+ true duplicates. Claims without duplicates must NOT appear.

```json
{
  "duplicate_groups": [
    {"keep": "claim_id", "drop": ["claim_id", ...], "reason": "why these are interchangeable and express the same proposition"}
  ]
}
```

## Final instruction
Be conservative.
False negatives are much better than false positives.
If there is any real chance that two claims represent different steps, different moves, or different usable beats, keep both.
"""


async def _semantic_dedupe_haiku(
    label: str,
    claims_pool: list,
    contexts: dict[str, dict],
    already_dropped: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Ask Haiku to find duplicate clusters. Returns list of (keep_id, drop_id, reason) tuples.

    Does NOT touch the DB — caller applies merges sequentially.
    Uses short IDs (c1, c2, …) in the LLM prompt to reduce token waste and typos.
    """
    from app.pipeline.short_ids import ShortIdMap, shorten_claim_dicts

    claim_payloads = []
    for claim in claims_pool:
        cid = str(claim.id)
        if cid in already_dropped:
            continue
        claim_payloads.append({
            "id": cid,
            "statement": claim.statement,
            "evidence_count": len(contexts.get(cid, {}).get("evidence_ids", set())),
        })

    if len(claim_payloads) < MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE:
        return []

    sid = ShortIdMap([p["id"] for p in claim_payloads])
    short_payloads = shorten_claim_dicts(claim_payloads, sid)

    prompt = json.dumps({"claims": short_payloads}, ensure_ascii=False)

    try:
        raw = await call_haiku_structured(
            system=_DEDUPE_SYSTEM,
            prompt=prompt,
            max_tokens=4096,
        )
        result = parse_llm_json(raw)
    except StepExecutionStopped:
        raise
    except Exception as exc:
        logger.warning("Haiku semantic dedup failed for %s: %s", label, exc)
        return []

    if not isinstance(result, dict):
        return []

    merges: list[tuple[str, str, str]] = []

    for group in result.get("duplicate_groups", []):
        if not isinstance(group, dict):
            continue
        raw_keep = str(group.get("keep", "")).strip()
        raw_drops = [str(d).strip() for d in group.get("drop", []) if isinstance(d, str)]

        # Restore short IDs back to UUIDs
        keep_id = sid.restore(raw_keep)
        drop_ids = [sid.restore(d) for d in raw_drops]

        if keep_id not in sid.long_ids:
            continue
        # Filter: valid, not self, not already dropped by earlier group in this batch
        already_merged_this_batch = {d for _, d, _ in merges}
        drop_ids = [
            d for d in drop_ids
            if d in sid.long_ids and d != keep_id
            and d not in already_dropped and d not in already_merged_this_batch
        ]

        if not drop_ids:
            continue

        reason = str(group.get("reason", ""))[:200]
        for drop_id in drop_ids:
            merges.append((keep_id, drop_id, reason))

    return merges


def _dump_dedupe_debug(
    *,
    run_id: str,
    execution_context: dict[str, object],
    requested_claim_id_count: int,
    loaded_claim_row_count: int,
    missing_claim_id_count: int,
    missing_claim_ids: list[str],
    lexical_dropped: int,
    semantic_dropped: int,
    kept_count: int,
    duplicate_map: dict[str, str],
    claims: list,
    contexts: dict[str, dict],
    semantic_branch_details: list[dict],
    rebuilt_claim_ids: set[str],
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return

    # Build per-claim summary (kept vs dropped)
    claim_summaries = []
    for claim in claims:
        cid = str(claim.id)
        ctx = contexts.get(cid, {})
        meta = decode_claim_notes_metadata(getattr(claim, "notes", "") or "")
        claim_summaries.append({
            "id": cid,
            "statement": claim.statement[:300] if hasattr(claim, "statement") else "",
            "branch_id": meta.get("builder_branch_id", "_unknown"),
            "claim_type": ctx.get("claim_type", ""),
            "status": "dropped" if cid in duplicate_map else "kept",
            "merged_into": duplicate_map.get(cid),
            "evidence_count": len(ctx.get("evidence_ids", set())),
            "source_count": len(ctx.get("source_ids", set())),
            "branch_ids": sorted(ctx.get("branch_ids", set())),
        })

    # Lexical merge pairs
    lexical_merges = [
        {
            "dropped": drop_id,
            "kept": keep_id,
            "dropped_stmt": next(
                (claim.statement[:200] for claim in claims if str(claim.id) == drop_id), ""
            ),
            "kept_stmt": next(
                (claim.statement[:200] for claim in claims if str(claim.id) == keep_id), ""
            ),
        }
        for drop_id, keep_id in duplicate_map.items()
        if drop_id not in {
            d["dropped_ids"][j]
            for d in semantic_branch_details
            if "dropped_ids" in d
            for j in range(len(d["dropped_ids"]))
        }
    ]

    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="dedupe",
        filename_stem=f"run-{safe_filename_token(run_id)}_summary",
        payload={
            "run_id": run_id,
            "execution_context": execution_context,
            "requested_claim_id_count": requested_claim_id_count,
            "loaded_claim_row_count": loaded_claim_row_count,
            "missing_claim_id_count": missing_claim_id_count,
            "missing_claim_ids": missing_claim_ids,
            "lexical_dropped": lexical_dropped,
            "semantic_dropped": semantic_dropped,
            "kept_count": kept_count,
            "lexical_merges": lexical_merges,
            "semantic_branches": semantic_branch_details,
            "rebuilt_claim_id_count": len(rebuilt_claim_ids),
            "live_branch_ids": sorted({
                str(meta.get("builder_branch_id", "")).strip()
                for claim in claims
                for meta in [decode_claim_notes_metadata(getattr(claim, "notes", "") or "")]
                if str(meta.get("builder_branch_id", "")).strip()
            }),
            "claims": claim_summaries,
        },
    )


def _semantic_batch_sort_key(claim, contexts: dict[str, dict]) -> tuple[str, str, str]:
    claim_id = str(claim.id)
    ctx = contexts.get(claim_id, {})
    return (
        str(ctx.get("normalized_statement", "")).strip(),
        ",".join(sorted(ctx.get("numbers_and_dates", set()))),
        claim_id,
    )


def _build_semantic_batches(claims: list, contexts: dict[str, dict]) -> list[list]:
    if len(claims) < MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE:
        return []
    ordered_claims = sorted(claims, key=lambda claim: _semantic_batch_sort_key(claim, contexts))
    if len(ordered_claims) <= MAX_CLAIMS_PER_HAIKU_CALL:
        return [ordered_claims]

    batches: list[list] = []
    start = 0
    while start < len(ordered_claims):
        batch = ordered_claims[start:start + MAX_CLAIMS_PER_HAIKU_CALL]
        if len(batch) >= MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE:
            batches.append(batch)
        if start + MAX_CLAIMS_PER_HAIKU_CALL >= len(ordered_claims):
            break
        start += SEMANTIC_BATCH_STRIDE
    return batches


def _resolve_live_claim_id(claim_id: str, duplicate_map: dict[str, str]) -> str:
    current = claim_id
    seen: set[str] = set()
    while current in duplicate_map and current not in seen:
        seen.add(current)
        current = duplicate_map[current]
    return current


def _prefer_keep_id(left_id: str, right_id: str, contexts: dict[str, dict]) -> str:
    def _score(claim_id: str) -> tuple[int, int, str]:
        ctx = contexts.get(claim_id, {})
        return (
            len(ctx.get("evidence_ids", set()) or set()),
            len(str(ctx.get("normalized_statement", "") or "")),
            claim_id,
        )

    return left_id if _score(left_id) >= _score(right_id) else right_id


def _consolidate_cross_branch_suggestions(
    suggested_merges: Iterable[dict[str, object]],
    contexts: dict[str, dict],
) -> list[dict[str, object]]:
    grouped: dict[frozenset[str], list[dict[str, object]]] = defaultdict(list)
    for merge in suggested_merges:
        keep_id = str(merge.get("keep_id", "")).strip()
        drop_id = str(merge.get("drop_id", "")).strip()
        if not keep_id or not drop_id or keep_id == drop_id:
            continue
        grouped[frozenset({keep_id, drop_id})].append(dict(merge))

    consolidated: list[dict[str, object]] = []
    for pair_key, items in grouped.items():
        pair = sorted(pair_key)
        if len(pair) != 2:
            continue
        left_id, right_id = pair
        directions = {
            (str(item.get("keep_id", "")).strip(), str(item.get("drop_id", "")).strip())
            for item in items
        }
        if len(directions) == 1:
            chosen_keep, chosen_drop = next(iter(directions))
        else:
            chosen_keep = _prefer_keep_id(left_id, right_id, contexts)
            chosen_drop = right_id if chosen_keep == left_id else left_id
        batch_indexes = sorted({
            int(item.get("batch_index", -1))
            for item in items
            if isinstance(item.get("batch_index"), int)
        })
        reasons = [
            str(item.get("reason", "")).strip()
            for item in items
            if str(item.get("reason", "")).strip()
        ]
        consolidated.append({
            "keep_id": chosen_keep,
            "drop_id": chosen_drop,
            "reason": reasons[0] if reasons else "",
            "batch_indexes": batch_indexes,
            "suggestion_count": len(items),
            "direction_conflict": len(directions) > 1,
            "keep_statement": str(contexts.get(chosen_keep, {}).get("normalized_statement", ""))[:300],
            "drop_statement": str(contexts.get(chosen_drop, {}).get("normalized_statement", ""))[:300],
        })

    consolidated.sort(
        key=lambda item: (
            -int(item.get("suggestion_count", 0) or 0),
            -len(contexts.get(str(item.get("keep_id", "")), {}).get("evidence_ids", set()) or set()),
            str(item.get("keep_id", "")),
            str(item.get("drop_id", "")),
        )
    )
    return consolidated

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
