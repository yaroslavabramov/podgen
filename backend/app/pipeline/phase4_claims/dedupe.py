"""Phase 4.15: deterministic global claim dedupe before verification."""

import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict

from sqlalchemy import bindparam, select, text, update

from app.models.claim import Claim, ClaimEvidence
from app.models.evidence import EvidenceProvenance, EvidenceSpan
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
            # Apply merges sequentially (safe for single db session)
            for keep_id, drop_id, reason in result:
                logger.info("Semantic dedup (branch %s): merging %s into %s — %s",
                            bid, drop_id[:12], keep_id[:12], reason)
                await _merge_claims(db, keep_claim_id=uuid.UUID(keep_id), drop_claim_id=uuid.UUID(drop_id))
                duplicate_map[drop_id] = keep_id
                merged_into_claim_ids.add(keep_id)
                all_dropped.append(drop_id)
            if branch_log is not None:
                dropped_ids = [d for _, d, _ in result]
                branch_log.append({
                    "branch_id": bid,
                    "claim_count": len(branch_groups.get(bid, [])),
                    "dropped_count": len(dropped_ids),
                    "dropped_ids": dropped_ids,
                })

    # --- Cross-branch pass: dedupe across all branches (batched) ---
    cross_surviving = [c for c in surviving_claims if str(c.id) not in duplicate_map]
    if len(cross_surviving) >= MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE:
        cross_dropped: list[str] = []
        batches = _build_semantic_batches(cross_surviving, contexts)

        if batches:
            cross_haiku_tasks = [
                _semantic_dedupe_haiku(
                    f"_cross_branch_{j}", batch, contexts, duplicate_map,
                )
                for j, batch in enumerate(batches)
            ]
            cross_haiku_results = await asyncio.gather(*cross_haiku_tasks, return_exceptions=True)
            for result in cross_haiku_results:
                if isinstance(result, StepExecutionStopped):
                    raise result
                if isinstance(result, Exception):
                    logger.warning("Semantic dedup cross-branch batch failed: %s", result)
                    continue
                for keep_id, drop_id, reason in result:
                    if drop_id in duplicate_map:
                        continue
                    logger.info("Semantic dedup (cross-branch): merging %s into %s — %s",
                                drop_id[:12], keep_id[:12], reason)
                    await _merge_claims(db, keep_claim_id=uuid.UUID(keep_id), drop_claim_id=uuid.UUID(drop_id))
                    duplicate_map[drop_id] = keep_id
                    merged_into_claim_ids.add(keep_id)
                    all_dropped.append(drop_id)
                    cross_dropped.append(drop_id)

        if branch_log is not None:
            branch_log.append({
                "branch_id": "_cross_branch",
                "claim_count": len(cross_surviving),
                "batch_count": len(batches),
                "dropped_count": len(cross_dropped),
                "dropped_ids": cross_dropped,
            })

    return all_dropped


_DEDUPE_SYSTEM = """\
You are a strict claim deduplication assistant for a podcast research pipeline.
Your job is to find claims that are TRUE duplicates — near-identical restatements of the same proposition.
Return JSON only.

## What counts as a duplicate
Two claims are duplicates ONLY if they assert the same core proposition and a podcast script would never need both.
Rephrasings, translations, and trivially reworded restatements are duplicates.

## What is NOT a duplicate — do NOT merge these
- Different facts about the same topic (e.g. "the peg was introduced in 1983" vs "the peg survived the 1997 handover")
- A general rule vs a specific mechanism (e.g. "currency board has two rules" vs "banks must deposit USD to issue HKD")
- Cause vs consequence (e.g. "currency board prevents monetary policy" vs "peg means Fed influences HK rates")
- Different historical events, even if related (e.g. "Oriental Bank issued first notes in 1846" vs "HSBC began issuing notes in 1865")
- A problem/precondition vs the institutional answer to that problem (e.g. "currency chaos existed" vs "authorities introduced a standard currency")
- One regime/state vs the later transition away from it (e.g. "the currency floated" vs "a peg was introduced later")
- A legal rule vs its later application, restoration, or enforcement outcome
- A fact vs an opinion or evaluation about that fact
- A claim that adds a date, number, name, or detail not present in the other

When in doubt, do NOT merge. It is much better to keep two similar claims than to lose a unique fact.

## Output format
For each duplicate group, pick the ONE best claim (most complete, highest evidence_count).
Return ONLY groups with 2+ true duplicates. Claims without duplicates must NOT appear.

```json
{
  "duplicate_groups": [
    {"keep": "claim_id", "drop": ["claim_id", ...], "reason": "why these are the same proposition"}
  ]
}
```"""


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
