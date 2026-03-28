"""Phase 4.1: Claim Builder — cluster evidence into typed claims."""

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.claim import Claim, ClaimEvidence
from app.models.generation_run import GenerationRun
from app.models.source import Source
from app.pipeline.phase3_research.normalization import normalize_research_text
from app.pipeline.phase4_claims.claim_notes import decode_claim_notes_metadata, encode_claim_notes_metadata
from app.pipeline.phase4_claims.claim_type_utils import normalize_claim_type
from app.pipeline.short_ids import ShortIdMap, restore_id_list
from app.pipeline.research_context import build_question_context_map
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, claim_created, progress

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[4]
CLAIM_BATCH_CHAR_BUDGET = 14000
CLAIM_BUILDER_MAX_QUOTE_CHARS = 3200

BUILD_CLAIMS_SYSTEM = """You are a claim builder for a fact-checked podcast pipeline.
Given evidence spans, produce the smallest high-value, non-redundant set of claims that still preserves distinct downstream narrative moves needed by verification, planning, and writing.

Claims are not an exhaustive inventory of every extractable fact fragment.
Prefer fewer, stronger, writer-usable claims over many thin micro-claims.

Each claim must have:
- claim_type: "FACT" (verifiable), "OPINION" (someone's view), "REPORT" (recount of event)
- statement: the claim in the target podcast language
- attribution: for OPINION/REPORT, who said it {who, where, when}
- supporting_evidence_ids: list of evidence IDs that support this
- illustrative_evidence_ids: list of evidence IDs for narrative value (ANECDOTE, CASE, etc.)

Return JSON array:
[
  {
    "claim_type": "FACT|OPINION|REPORT",
    "statement": "claim text in target language",
    "attribution": {"who": "", "where": "", "when": ""} or null,
    "supporting_evidence_ids": ["id1", "id2"],
    "illustrative_evidence_ids": ["id3"]
  }
]

Rules:
- One claim should express one semantically clean, writer-usable proposition that can be verified as a unit.
- Do not combine unrelated propositions in one statement, but do not split one coherent proposition into multiple micro-claims just because the evidence mentions several details.
- Do not create one claim per evidence span by default. Merge evidence whenever spans support the same proposition core, even if they differ in wording, emphasis, examples, or supporting detail.
- Split only when the pieces would matter independently to verification, planning, or writing, or when one could be true while the other false.
- Preserve distinct downstream moves. Do NOT merge setup and response, problem and remedy, one regime and a later regime shift, or a legal rule and its later application/result if those would naturally become different planning beats.
- Do NOT compress a chronology into one claim when downstream planning would naturally stage those historical turns separately.
- Treat dates, numbers, actors, examples, qualifiers, and consequences as supporting detail unless they materially change the proposition's downstream meaning.
- Do not add facts, entities, numbers, dates, motives, or causal links that are not explicitly present in evidence.
- If a quote is partial, truncated, or only indirectly relevant, keep the claim equally narrow. Do not "complete" the missing part from background knowledge.
- Do not upgrade contextual framing into a stronger factual claim unless the proposition is explicitly carried by the supporting quote.
- FACT must be directly supported by supporting_evidence_ids.
- OPINION/REPORT must have attribution with at least "who" when available.
- Deduplicate within this batch: if two candidates are semantic paraphrases or micro-variants of the same proposition, keep one claim and merge evidence IDs.
- Prefer concise statements in the target language, usually one sentence; two tightly connected clauses are acceptable only when they restate one proposition with necessary precision, not when they encode a sequence, setup->response, or cause->effect chain.
- CASE, ANECDOTE, and EXPERT_QUOTE default to illustrative evidence unless they explicitly carry a standalone proposition that would be useful independently downstream.
- For debate questions, preserve explicit arguments, tradeoffs, criticisms, and attributed positions. Prefer one argumentative position as one claim when possible; do not fragment one position into multiple narrow claims unless the sub-points matter independently.
- For compare questions, preserve explicit differences, alternatives, and contrastive framing. Prefer one meaningful contrast as one claim when possible; do not create separate claims for every compared attribute unless each attribute matters independently.
- For case_study questions, prefer concrete actors, actions, outcomes, and consequences from the episode itself. Preserve one coherent episode or outcome as one claim when possible; do not fragment actor, action, and result into separate claims unless they need to stand alone downstream.
- The same evidence may appear in both supporting_evidence_ids and illustrative_evidence_ids.
- Use source metadata, branch/question intent, lineage, direct_or_contextual, and contextual_asset_types to avoid mixing different research branches into one claim.
- Treat secondary/provenance branch contexts as background only. They are not permission to restate another branch's explanatory backbone as a fresh claim in this batch.
- Shape examples:
  - Bad: "A disorder persisted, so authorities introduced a standard rule."
    Good: split into one claim for the disorder/precondition and one claim for the later intervention/response.
  - Bad: "A mechanism required backing, and later market prices self-corrected."
    Good: split when one clause is the operating constraint and the other is a later adjustment mechanism.
  - Bad: "A rule was enacted, and later authorities restored it after a crisis."
    Good: split the legal rule from the later restoration/application outcome.
- When unsure whether to split or merge, prefer the narrower or split version. Merge only when the result is clearly one proposition that would stay together downstream.
- Use only evidence IDs that are present in the input.
- Evidence IDs are opaque labels. Copy them exactly from the input instead of rewriting or guessing them.
- Return raw JSON only. Do not use markdown/code fences or extra prose."""


async def build_claims(state: PipelineState) -> dict:
    """LangGraph node: build claims from evidence."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase4_claims", "Building and categorizing claims"))
    update_generation_progress(run_id, "phase4_claims", "claim_builder", 35.0)

    evidence_ids = list(dict.fromkeys(state.get("evidence_ids", [])))

    all_claim_ids: list[str] = []
    rebuilt_claim_ids: list[str] = []
    preserved_claim_ids: list[str] = []
    dropped_stale_preserved_claim_ids: list[str] = []
    dedupe_source_claims: list[dict] = []
    rebuild_scope = _derive_targeted_rebuild_scope(state)
    rebuild_mode = "full_rebuild"
    manual_step_rerun = False
    manual_full_rebuild_claim_ids: list[str] = []
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
    previous_rebuild_lookup: dict[str, list[dict]] = {}

    async with async_session_factory() as db:
        manual_step_rerun = await _should_force_full_rebuild_for_node_rerun(
            db,
            generation_run_id=state["generation_run_id"],
            node_name="build_claims",
        )
        if manual_step_rerun:
            rebuild_scope = None
            manual_full_rebuild_claim_ids = list(
                await _load_live_claim_rerun_metadata(
                    db=db,
                    claim_ids=list(dict.fromkeys(state.get("claim_ids", []))),
                )
            )
        if not evidence_ids:
            if manual_full_rebuild_claim_ids:
                await _delete_claims(db, manual_full_rebuild_claim_ids)
            return {
                "claim_ids": [],
                "rebuilt_claim_ids": [],
                "preserved_claim_ids": [],
                "dropped_stale_preserved_claim_ids": [],
                "targeted_branch_ids": [],
                "rebuild_mode": rebuild_mode,
                "live_branch_ids": [],
                "dedupe_source_claims": [],
                "current_phase": "phase4_claims",
            }
        question_context_map: dict[str, dict] = {}
        if state.get("manifest_id"):
            manifest_result = await db.execute(
                select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
            )
            manifest = manifest_result.scalar_one_or_none()
            question_context_map = build_question_context_map(
                manifest.research_tree_json if manifest else None
            )
        evidence_index, branch_batches = await _load_branch_batches(
            db=db,
            evidence_ids=evidence_ids,
            generation_run_id=state["generation_run_id"],
            question_context_map=question_context_map,
        )
        current_branch_ids, current_question_ids_by_branch = _derive_current_branch_scope(
            branch_batches,
            question_context_map,
        )
        if not evidence_index:
            if manual_full_rebuild_claim_ids:
                await _delete_claims(db, manual_full_rebuild_claim_ids)
            return {
                "claim_ids": [],
                "rebuilt_claim_ids": [],
                "preserved_claim_ids": [],
                "dropped_stale_preserved_claim_ids": [],
                "targeted_branch_ids": rebuild_scope["branch_ids"] if rebuild_scope is not None else [],
                "rebuild_mode": rebuild_mode,
                "live_branch_ids": sorted(current_branch_ids),
                "dedupe_source_claims": [],
                "current_phase": "phase4_claims",
            }
        if rebuild_scope is not None:
            live_claim_metadata = await _load_live_claim_rerun_metadata(
                db=db,
                claim_ids=list(dict.fromkeys(state.get("claim_ids", []))),
            )
            if _should_fallback_to_full_rebuild(live_claim_metadata, current_branch_ids):
                impacted_claim_ids = list(live_claim_metadata)
                preserved_candidate_ids: list[str] = []
                rebuild_mode = "full_rebuild"
            else:
                rebuild_mode = "targeted_rebuild_with_revalidation"
                branch_batches = _filter_branch_batches(branch_batches, rebuild_scope["branch_ids"])
                impacted_claim_ids, preserved_candidate_ids = await _partition_claim_ids_for_targeted_rebuild(
                    db=db,
                    claim_ids=list(live_claim_metadata),
                    targeted_branch_ids=rebuild_scope["branch_ids"],
                    explicit_claim_ids=rebuild_scope["claim_ids"],
                )
                preserved_claim_ids, dropped_stale_preserved_claim_ids = _revalidate_preserved_claim_ids(
                    preserved_candidate_ids,
                    live_claim_metadata=live_claim_metadata,
                    current_branch_ids=current_branch_ids,
                    current_question_ids_by_branch=current_question_ids_by_branch,
                )
                if not branch_batches:
                    dedupe_source_claims = await _snapshot_claim_pool_for_dedupe(
                        db=db,
                        claim_ids=preserved_claim_ids,
                    )
                    return {
                        "claim_ids": preserved_claim_ids,
                        "rebuilt_claim_ids": [],
                        "preserved_claim_ids": preserved_claim_ids,
                        "dropped_stale_preserved_claim_ids": dropped_stale_preserved_claim_ids,
                        "targeted_branch_ids": rebuild_scope["branch_ids"],
                        "rebuild_mode": rebuild_mode,
                        "live_branch_ids": sorted(current_branch_ids),
                        "dedupe_source_claims": dedupe_source_claims,
                        "current_phase": "phase4_claims",
                    }
            if impacted_claim_ids:
                previous_rebuild_lookup = await _load_previous_claim_rebuild_state(
                    db=db,
                    claim_ids=impacted_claim_ids,
                    previous_gap_claim_ids=previous_gap_claim_ids,
                )
            if impacted_claim_ids:
                await _delete_claims(db, impacted_claim_ids)
            all_claim_ids.extend(preserved_claim_ids)
        elif manual_full_rebuild_claim_ids:
            await _delete_claims(db, manual_full_rebuild_claim_ids)

        publish_event(
            run_id,
            step_started(
                "phase4_claims",
                "cluster_evidence",
                (
                    f"Clustering {sum(len(group) for _, group in branch_batches)} evidence spans "
                    f"across {len(branch_batches)} branch groups"
                    + (" (targeted rebuild)" if rebuild_scope is not None else "")
                ),
            ),
        )

        batch_number = 0
        for branch_id, evidence_group in branch_batches:
            for batch in _split_evidence_batch(evidence_group):
                batch_number += 1
                batch_ids = {e["id"] for e in batch}
                batch_evidence_index = {str(item["id"]): item for item in batch}
                branch_context = _summarize_branch_context(branch_id, batch, question_context_map)
                evidence_sid = ShortIdMap(list(batch_ids), prefix="e")
                payload = {
                    "build_mode": rebuild_mode,
                    "targeted_branch_ids": rebuild_scope["branch_ids"] if rebuild_scope is not None else [],
                    "branch_id": branch_id,
                    "branch_context": branch_context,
                    "evidence_spans": _shorten_claim_builder_evidence_spans(batch, evidence_sid),
                    "target_language": state["language"],
                    "instructions": _build_claim_builder_instructions(branch_context),
                }

                prompt = json.dumps(payload, ensure_ascii=False)
                raw = ""
                try:
                    raw = await call_sonnet_structured(
                        system=BUILD_CLAIMS_SYSTEM,
                        prompt=prompt,
                        max_tokens=8192,
                        temperature=0.4,
                    )
                    claims_data = _restore_claim_builder_evidence_ids(
                        parse_llm_json(raw),
                        evidence_sid=evidence_sid,
                    )
                except Exception:
                    logger.error("Claim building failed", exc_info=True)
                    _dump_claim_builder_batch_debug(
                        run_id=run_id,
                        batch_number=batch_number,
                        status="llm_or_parse_failed",
                        input_payload=payload,
                        llm_raw_output=raw,
                        parsed_claims=None,
                        saved_claims=[],
                        persist_stats={},
                    )
                    continue

                if not isinstance(claims_data, list):
                    logger.warning("Claim builder returned non-list payload for batch %s", batch_number)
                    _dump_claim_builder_batch_debug(
                        run_id=run_id,
                        batch_number=batch_number,
                        status="invalid_claims_payload",
                        input_payload=payload,
                        llm_raw_output=raw,
                        parsed_claims=claims_data,
                        saved_claims=[],
                        persist_stats={},
                    )
                    continue

                saved_claims_for_batch, persist_stats = await _persist_claim_batch(
                    db=db,
                    project_id=state["project_id"],
                    claims_data=claims_data,
                    batch_ids=batch_ids,
                    branch_id=branch_id,
                    branch_question_ids=branch_context.get("question_ids", []),
                    batch_evidence_index=batch_evidence_index,
                    previous_rebuild_lookup=previous_rebuild_lookup,
                    run_id=run_id,
                    all_claim_ids=all_claim_ids,
                )
                rebuilt_claim_ids.extend(claim["claim_id"] for claim in saved_claims_for_batch if claim.get("claim_id"))
                _dump_claim_builder_batch_debug(
                    run_id=run_id,
                    batch_number=batch_number,
                    status="ok",
                    input_payload=payload,
                    llm_raw_output=raw,
                    parsed_claims=claims_data,
                    saved_claims=saved_claims_for_batch,
                    persist_stats=persist_stats,
                )

        await db.commit()
        dedupe_source_claims = await _snapshot_claim_pool_for_dedupe(
            db=db,
            claim_ids=list(dict.fromkeys(all_claim_ids)),
        )

    publish_event(run_id, step_completed("phase4_claims", "cluster_evidence"))
    publish_event(run_id, progress(40.0, f"Created {len(all_claim_ids)} claims from evidence"))
    update_generation_progress(run_id, "phase4_claims", "claim_builder_done", 40.0)

    if rebuild_scope is None:
        rebuilt_claim_ids = list(all_claim_ids)
        preserved_claim_ids = []

    return {
        "claim_ids": list(dict.fromkeys(all_claim_ids)),
        "rebuilt_claim_ids": list(dict.fromkeys(rebuilt_claim_ids)),
        "preserved_claim_ids": list(dict.fromkeys(preserved_claim_ids)),
        "dropped_stale_preserved_claim_ids": list(dict.fromkeys(dropped_stale_preserved_claim_ids)),
        "targeted_branch_ids": rebuild_scope["branch_ids"] if rebuild_scope is not None else [],
        "rebuild_mode": rebuild_mode,
        "live_branch_ids": sorted(current_branch_ids),
        "dedupe_source_claims": dedupe_source_claims,
        "current_phase": "phase4_claims",
    }


def _derive_targeted_rebuild_scope(state: PipelineState) -> dict | None:
    iteration_counts = state.get("iteration_counts", {}) or {}
    if iteration_counts.get("verify_research_loops", 0) < 1:
        return None

    gaps = state.get("research_gaps", []) or []
    branch_ids: set[str] = set()
    claim_ids: set[str] = set()
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        branch_id = str(gap.get("branch_id", "")).strip()
        if branch_id:
            branch_ids.add(branch_id)
        for context in (gap.get("question_contexts", []) or []):
            if not isinstance(context, dict):
                continue
            contextual_branch_id = str(context.get("branch_id", "")).strip()
            if contextual_branch_id:
                branch_ids.add(contextual_branch_id)
        gap_claim_ids = [
            str(claim_id).strip()
            for claim_id in (gap.get("claim_ids", []) or [])
            if str(claim_id).strip()
        ]
        if gap_claim_ids:
            claim_ids.update(gap_claim_ids)
        claim_id = str(gap.get("claim_id", "")).strip()
        if claim_id:
            claim_ids.add(claim_id)
    if not branch_ids and not claim_ids:
        return None
    return {
        "branch_ids": sorted(branch_ids),
        "claim_ids": sorted(claim_ids),
    }


def _filter_branch_batches(
    branch_batches: list[tuple[str, list[dict]]],
    targeted_branch_ids: list[str],
) -> list[tuple[str, list[dict]]]:
    if not targeted_branch_ids:
        return branch_batches
    targeted = set(targeted_branch_ids)
    return [(branch_id, items) for branch_id, items in branch_batches if branch_id in targeted]


async def _partition_claim_ids_for_targeted_rebuild(
    *,
    db,
    claim_ids: list[str],
    targeted_branch_ids: list[str],
    explicit_claim_ids: list[str],
) -> tuple[list[str], list[str]]:
    ordered_claim_ids = list(dict.fromkeys(claim_ids))
    impacted_claim_ids = {
        claim_id
        for claim_id in explicit_claim_ids
        if claim_id in ordered_claim_ids
    }
    if targeted_branch_ids and ordered_claim_ids:
        claim_uuid_ids = [uuid.UUID(claim_id) for claim_id in ordered_claim_ids]
        result = await db.execute(select(Claim.id, Claim.notes).where(Claim.id.in_(claim_uuid_ids)))
        for claim_id, notes in result.all():
            builder_branch_id = str(decode_claim_notes_metadata(notes).get("builder_branch_id", "")).strip()
            if builder_branch_id and builder_branch_id in targeted_branch_ids:
                impacted_claim_ids.add(str(claim_id))

    impacted = [claim_id for claim_id in ordered_claim_ids if claim_id in impacted_claim_ids]
    preserved = [claim_id for claim_id in ordered_claim_ids if claim_id not in impacted_claim_ids]
    return impacted, preserved


async def _load_live_claim_rerun_metadata(
    *,
    db,
    claim_ids: list[str],
) -> dict[str, dict]:
    ordered_claim_ids = list(dict.fromkeys(claim_ids))
    if not ordered_claim_ids:
        return {}

    claim_uuid_ids: list[uuid.UUID] = []
    valid_by_str: dict[str, uuid.UUID] = {}
    for claim_id in ordered_claim_ids:
        try:
            parsed = uuid.UUID(claim_id)
        except (TypeError, ValueError):
            logger.warning("Skipping invalid claim ID in rerun metadata lookup: %s", claim_id)
            continue
        claim_uuid_ids.append(parsed)
        valid_by_str[str(parsed)] = parsed
    if not claim_uuid_ids:
        return {}

    result = await db.execute(select(Claim.id, Claim.notes).where(Claim.id.in_(claim_uuid_ids)))
    metadata: dict[str, dict] = {}
    for claim_id, notes in result.all():
        claim_id_str = str(claim_id)
        decoded = decode_claim_notes_metadata(notes)
        metadata[claim_id_str] = {
            "builder_branch_id": str(decoded.get("builder_branch_id", "")).strip(),
            "builder_question_ids": [
                str(question_id).strip()
                for question_id in (decoded.get("builder_question_ids", []) or [])
                if str(question_id).strip()
            ],
            "has_live_evidence": False,
        }

    if metadata:
        evidence_result = await db.execute(
            select(ClaimEvidence.claim_id).where(
                ClaimEvidence.claim_id.in_([valid_by_str[cid] for cid in metadata])
            )
        )
        for (claim_id,) in evidence_result.all():
            claim_id_str = str(claim_id)
            if claim_id_str in metadata:
                metadata[claim_id_str]["has_live_evidence"] = True

    return {
        claim_id: metadata[claim_id]
        for claim_id in ordered_claim_ids
        if claim_id in metadata
    }


def _should_force_full_rebuild_for_config(config: dict | None, *, node_name: str) -> bool:
    if not isinstance(config, dict):
        return False
    if str(config.get("execution_mode") or "").strip().lower() != "step":
        return False
    step_control = config.get("step_control")
    if not isinstance(step_control, dict):
        return False
    if step_control.get("requested_action") != "rerun":
        return False
    requested_target_node = step_control.get("requested_target_node")
    if isinstance(requested_target_node, str) and requested_target_node.strip():
        return requested_target_node.strip() == node_name
    last_node = step_control.get("last_node")
    return isinstance(last_node, str) and last_node == node_name


async def _should_force_full_rebuild_for_node_rerun(
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
    return _should_force_full_rebuild_for_config(result.scalar_one_or_none(), node_name=node_name)


def _derive_current_branch_scope(
    branch_batches: list[tuple[str, list[dict]]],
    _question_context_map: dict[str, dict],
) -> tuple[set[str], dict[str, set[str]]]:
    branch_ids = {
        str(branch_id).strip()
        for branch_id, _ in branch_batches
        if str(branch_id).strip()
    }
    question_ids_by_branch: dict[str, set[str]] = {
        str(branch_id).strip(): set()
        for branch_id, _ in branch_batches
        if str(branch_id).strip()
    }

    for branch_id, items in branch_batches:
        canonical_branch_id = str(branch_id).strip()
        for item in items:
            lineage = item.get("lineage", {}) if isinstance(item, dict) else {}
            for context in lineage.get("question_contexts", []):
                if not isinstance(context, dict):
                    continue
                qid = str(context.get("question_id", "")).strip()
                bid = str(context.get("branch_id", "")).strip() or canonical_branch_id
                if qid:
                    question_ids_by_branch.setdefault(bid, set()).add(qid)
                    branch_ids.add(bid)
                if bid:
                    branch_ids.add(bid)
                    question_ids_by_branch.setdefault(bid, set())

    return branch_ids, question_ids_by_branch


def _should_fallback_to_full_rebuild(
    live_claim_metadata: dict[str, dict],
    current_branch_ids: set[str],
) -> bool:
    if not live_claim_metadata or not current_branch_ids:
        return False
    prior_branch_ids = {
        str(meta.get("builder_branch_id", "")).strip()
        for meta in live_claim_metadata.values()
        if str(meta.get("builder_branch_id", "")).strip()
    }
    return bool(prior_branch_ids - current_branch_ids)


def _revalidate_preserved_claim_ids(
    preserved_candidate_ids: list[str],
    *,
    live_claim_metadata: dict[str, dict],
    current_branch_ids: set[str],
    current_question_ids_by_branch: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    preserved: list[str] = []
    dropped: list[str] = []
    for claim_id in preserved_candidate_ids:
        meta = live_claim_metadata.get(claim_id)
        if not meta:
            dropped.append(claim_id)
            continue
        if not meta.get("has_live_evidence"):
            dropped.append(claim_id)
            continue
        branch_id = str(meta.get("builder_branch_id", "")).strip()
        if current_branch_ids and branch_id and branch_id not in current_branch_ids:
            dropped.append(claim_id)
            continue
        question_ids = [
            str(question_id).strip()
            for question_id in (meta.get("builder_question_ids", []) or [])
            if str(question_id).strip()
        ]
        if question_ids:
            branch_question_ids = current_question_ids_by_branch.get(branch_id, set())
            if not branch_question_ids:
                dropped.append(claim_id)
                continue
            if not any(question_id in branch_question_ids for question_id in question_ids):
                dropped.append(claim_id)
                continue
        preserved.append(claim_id)
    return preserved, dropped


async def _delete_claims(db, claim_ids: list[str]) -> None:
    if not claim_ids:
        return
    claim_uuid_ids = [uuid.UUID(claim_id) for claim_id in claim_ids]
    result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuid_ids)))
    for claim in result.scalars().all():
        await db.delete(claim)
    await db.flush()


async def _load_previous_claim_rebuild_state(
    *,
    db,
    claim_ids: list[str],
    previous_gap_claim_ids: set[str],
) -> dict[str, list[dict]]:
    if not claim_ids:
        return {}

    claim_uuid_ids: list[uuid.UUID] = []
    for claim_id in claim_ids:
        try:
            claim_uuid_ids.append(uuid.UUID(claim_id))
        except (TypeError, ValueError):
            logger.warning("Skipping invalid claim ID in previous rebuild lookup: %s", claim_id)
    if not claim_uuid_ids:
        return {}
    result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuid_ids)))
    claims = {str(claim.id): claim for claim in result.scalars().all()}
    evidence_result = await db.execute(
        select(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_uuid_ids))
    )
    links_by_claim: dict[str, list[ClaimEvidence]] = defaultdict(list)
    for link in evidence_result.scalars().all():
        links_by_claim[str(link.claim_id)].append(link)

    lookup: dict[str, list[dict]] = defaultdict(list)
    for claim_id, claim in claims.items():
        signature = _claim_rebuild_signature(
            statement=claim.statement,
            claim_type=claim.claim_type,
            support_ids={
                str(link.evidence_id)
                for link in links_by_claim.get(claim_id, [])
                if link.supports_fact
            },
            illustrative_ids={
                str(link.evidence_id)
                for link in links_by_claim.get(claim_id, [])
                if link.illustrative
            },
        )
        lookup[signature].append(
            {
                "claim_id": claim_id,
                "verification_level": claim.verification_level,
                "verification_fingerprint": claim.verification_fingerprint,
                "was_gap_claim": claim_id in previous_gap_claim_ids,
            }
        )
    return lookup


async def _snapshot_claim_pool_for_dedupe(
    *,
    db,
    claim_ids: list[str],
) -> list[dict]:
    ordered_claim_ids = list(dict.fromkeys(claim_ids))
    if not ordered_claim_ids:
        return []

    claim_uuid_ids: list[uuid.UUID] = []
    for claim_id in ordered_claim_ids:
        try:
            claim_uuid_ids.append(uuid.UUID(claim_id))
        except (TypeError, ValueError):
            logger.warning("Skipping invalid claim ID in dedupe snapshot: %s", claim_id)
    if not claim_uuid_ids:
        return []

    result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuid_ids)))
    claims_by_id = {str(claim.id): claim for claim in result.scalars().all()}
    evidence_result = await db.execute(
        select(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_uuid_ids))
    )
    links_by_claim: dict[str, list[ClaimEvidence]] = defaultdict(list)
    for link in evidence_result.scalars().all():
        links_by_claim[str(link.claim_id)].append(link)

    snapshots: list[dict] = []
    for claim_id in ordered_claim_ids:
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        snapshots.append(
            {
                "claim_id": claim_id,
                "claim_type": claim.claim_type,
                "verification_level": claim.verification_level,
                "verification_fingerprint": claim.verification_fingerprint,
                "statement": claim.statement,
                "attribution": claim.attribution,
                "as_of_date": claim.as_of_date.isoformat() if claim.as_of_date else None,
                "notes": claim.notes,
                "created_at": claim.created_at.isoformat() if claim.created_at else None,
                "updated_at": claim.updated_at.isoformat() if claim.updated_at else None,
                "evidence_links": [
                    {
                        "evidence_id": str(link.evidence_id),
                        "supports_fact": bool(link.supports_fact),
                        "contradicts_fact": bool(link.contradicts_fact),
                        "illustrative": bool(link.illustrative),
                    }
                    for link in sorted(
                        links_by_claim.get(claim_id, []),
                        key=lambda row: str(row.evidence_id),
                    )
                ],
            }
        )
    return snapshots


async def _load_branch_batches(
    *,
    db,
    evidence_ids: list[str],
    generation_run_id: str,
    question_context_map: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], list[tuple[str, list[dict]]]]:
    evidence_uuid_ids = [uuid.UUID(eid) for eid in evidence_ids]
    evidence_result = await db.execute(
        select(EvidenceSpan, Source)
        .join(Source, EvidenceSpan.source_id == Source.id)
        .where(EvidenceSpan.id.in_(evidence_uuid_ids))
    )

    evidence_index: dict[str, dict] = {}
    for evidence, source in evidence_result.all():
        evidence_index[str(evidence.id)] = {
            "id": str(evidence.id),
            "verbatim_quote": _prepare_claim_builder_quote(evidence.verbatim_quote),
            "canonical_asset_type": evidence.asset_type,
            "narrative_value_score": evidence.narrative_value_score,
            "reliability_hint": evidence.reliability_hint,
            "source_id": str(evidence.source_id),
            "source": {
                "title": source.title or "",
                "url": source.original_url or "",
                "type": source.source_type,
                "domain_reliability": source.domain_reliability,
                "authority_type": source.authority_type,
                "is_temporary": source.is_temporary,
                "published_at": source.published_at.isoformat() if source.published_at else None,
            },
            "lineage": {
                "branch_ids": [],
                "question_ids": [],
                "intents": [],
                "retrieved_by": [],
                "question_contexts": [],
            },
        }

    provenance_result = await db.execute(
        select(EvidenceProvenance).where(
            EvidenceProvenance.evidence_id.in_(evidence_uuid_ids),
            EvidenceProvenance.generation_run_id == uuid.UUID(generation_run_id),
        )
    )
    branch_batches = _build_branch_batches(
        evidence_index,
        list(provenance_result.scalars()),
        question_context_map=question_context_map or {},
    )
    return evidence_index, branch_batches


def _build_branch_batches(
    evidence_index: dict[str, dict],
    provenances: list[EvidenceProvenance],
    *,
    question_context_map: dict[str, dict] | None = None,
) -> list[tuple[str, list[dict]]]:
    branch_to_ids: dict[str, set[str]] = defaultdict(set)
    branch_asset_types: dict[tuple[str, str], set[str]] = defaultdict(set)
    question_context_map = question_context_map or {}

    for provenance in provenances:
        evidence_id = str(provenance.evidence_id)
        evidence_item = evidence_index.get(evidence_id)
        if evidence_item is None:
            continue
        lineage = evidence_item["lineage"]
        lineage["branch_ids"].append(provenance.branch_id)
        lineage["question_ids"].append(provenance.question_id)
        lineage["retrieved_by"].append(provenance.retrieved_by)
        question_context = question_context_map.get(provenance.question_id, {})
        intent = question_context.get("intent", "")
        if intent:
            lineage["intents"].append(intent)
        lineage["question_contexts"].append(
            {
                "branch_id": provenance.branch_id,
                "question_id": provenance.question_id,
                "question": question_context.get("question", ""),
                "intent": intent,
                "direct_or_contextual": provenance.direct_or_contextual,
            }
        )
        branch_to_ids[provenance.branch_id].add(evidence_id)
        branch_asset_types[(provenance.branch_id, evidence_id)].add(str(provenance.asset_type))

    for evidence_id, evidence_item in evidence_index.items():
        lineage = evidence_item["lineage"]
        lineage["branch_ids"] = sorted(set(lineage["branch_ids"]))
        lineage["question_ids"] = sorted(set(lineage["question_ids"]))
        lineage["intents"] = sorted(set(lineage["intents"]))
        lineage["retrieved_by"] = sorted(set(lineage["retrieved_by"]))
        lineage["question_contexts"] = sorted(
            {
                json.dumps(item, ensure_ascii=False, sort_keys=True): item
                for item in lineage["question_contexts"]
            }.values(),
            key=lambda item: (item.get("branch_id", ""), item.get("question_id", "")),
        )
        if not lineage["branch_ids"]:
            branch_to_ids["ungrouped"].add(evidence_id)

    branch_batches: list[tuple[str, list[dict]]] = []
    for branch_id, batch_ids in sorted(branch_to_ids.items(), key=lambda item: item[0]):
        items: list[dict] = []
        for evidence_id in batch_ids:
            base_item = evidence_index.get(evidence_id)
            if base_item is None:
                continue
            contextual_asset_types = sorted(
                branch_asset_types.get((branch_id, evidence_id), set())
            ) or [base_item["canonical_asset_type"]]
            items.append(
                {
                    **base_item,
                    "asset_type": contextual_asset_types[0],
                    "contextual_asset_types": contextual_asset_types,
                    "source": dict(base_item["source"]),
                    "lineage": {
                        "branch_ids": list(base_item["lineage"]["branch_ids"]),
                        "question_ids": list(base_item["lineage"]["question_ids"]),
                        "intents": list(base_item["lineage"]["intents"]),
                        "retrieved_by": list(base_item["lineage"]["retrieved_by"]),
                        "question_contexts": list(base_item["lineage"]["question_contexts"]),
                    },
                }
            )
        items.sort(key=lambda item: (item["lineage"]["question_ids"], item["id"]))
        if items:
            branch_batches.append((branch_id, items))
    return branch_batches


def _prepare_claim_builder_quote(quote: str) -> str:
    if len(quote) <= CLAIM_BUILDER_MAX_QUOTE_CHARS:
        return quote
    head = quote[:2000].rstrip()
    tail = quote[-1000:].lstrip()
    return f"{head}\n...[middle omitted for prompt budget]...\n{tail}"


def _shorten_claim_builder_evidence_spans(
    evidence_spans: list[dict[str, Any]],
    evidence_sid: ShortIdMap,
) -> list[dict[str, Any]]:
    shortened: list[dict[str, Any]] = []
    for item in evidence_spans:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if isinstance(copied.get("id"), str):
            copied["id"] = evidence_sid.shorten(copied["id"])
        shortened.append(copied)
    return shortened


def _restore_claim_builder_evidence_ids(
    claims_data: Any,
    *,
    evidence_sid: ShortIdMap,
) -> Any:
    if not isinstance(claims_data, list):
        return claims_data
    restored: list[Any] = []
    for item in claims_data:
        if not isinstance(item, dict):
            restored.append(item)
            continue
        copied = dict(item)
        copied["supporting_evidence_ids"] = restore_id_list(
            copied.get("supporting_evidence_ids"),
            evidence_sid,
            drop_unknown=True,
        )
        copied["illustrative_evidence_ids"] = restore_id_list(
            copied.get("illustrative_evidence_ids"),
            evidence_sid,
            drop_unknown=True,
        )
        restored.append(copied)
    return restored


def _split_evidence_batch(evidence_group: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_chars = 0
    for item in evidence_group:
        item_chars = _estimate_evidence_chars(item)
        if current_batch and current_chars + item_chars > CLAIM_BATCH_CHAR_BUDGET:
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(item)
        current_chars += item_chars
    if current_batch:
        batches.append(current_batch)
    return batches


def _estimate_evidence_chars(item: dict) -> int:
    source = item.get("source", {})
    lineage = item.get("lineage", {})
    return sum(
        len(str(value))
        for value in (
            item.get("verbatim_quote", ""),
            item.get("asset_type", ""),
            json.dumps(item.get("contextual_asset_types", []), ensure_ascii=False),
            source.get("title", "") if isinstance(source, dict) else "",
            source.get("url", "") if isinstance(source, dict) else "",
            json.dumps(lineage, ensure_ascii=False, sort_keys=True) if isinstance(lineage, dict) else "",
        )
    ) + 200


def _summarize_branch_context(
    branch_id: str,
    batch: list[dict],
    question_context_map: dict[str, dict],
) -> dict:
    branch_question = question_context_map.get(branch_id, {})
    primary_question_contexts: dict[str, dict] = {}
    secondary_question_contexts: dict[str, dict] = {}
    intents: set[str] = set()
    for item in batch:
        lineage = item.get("lineage", {})
        for context in lineage.get("question_contexts", []):
            if not isinstance(context, dict):
                continue
            question_id = str(context.get("question_id", "")).strip()
            if not question_id:
                continue
            context_branch_id = str(context.get("branch_id", "")).strip()
            if context_branch_id == branch_id:
                primary_question_contexts[question_id] = context
            else:
                secondary_question_contexts[question_id] = context
            intent = context.get("intent")
            if isinstance(intent, str) and intent:
                intents.add(intent)
    all_question_ids = sorted(set(primary_question_contexts) | set(secondary_question_contexts))
    return {
        "branch_id": branch_id,
        "branch_question": branch_question.get("question", ""),
        "branch_intent": branch_question.get("intent", ""),
        "question_ids": all_question_ids,
        "primary_question_ids": sorted(primary_question_contexts),
        "primary_question_contexts": [
            primary_question_contexts[qid] for qid in sorted(primary_question_contexts)
        ],
        "secondary_question_contexts": [
            secondary_question_contexts[qid] for qid in sorted(secondary_question_contexts)
        ],
        "intents": sorted(intents),
    }


def _build_claim_builder_instructions(branch_context: dict) -> str:
    base = (
        "Build a compact, non-duplicative set of writer-usable claims from these evidence spans. "
        "Claims are a factual backbone for downstream verification, planning, and writing, not an exhaustive inventory of every extractable fact fragment. "
        "Prefer fewer, stronger claims over many thin restatements. "
        "Do not turn every evidence span or supporting detail into its own claim. "
        "Merge spans that support the same proposition core, even when they add different details or examples. "
        "Split only when the parts would matter independently downstream or when one could be true without the other. "
        "Preserve one downstream move per claim. "
        "Do not merge setup and response, problem and remedy, one regime and a later regime shift, or a legal rule and its later application/result when those would naturally become separate planning beats. "
        "Do not compress a chronology into one claim when downstream planning would stage the turns separately. "
        "Do not introduce new facts. "
        "Preserve narrative evidence by using illustrative_evidence_ids. "
        "Use supporting_evidence_ids only when the quote directly supports the claim. "
        "Prefer direct evidence for factual backbone; use contextual evidence as framing or illustration unless it explicitly carries the proposition. "
        "If a quote is partial or truncated, keep the claim equally narrow. "
        "Do not infer dates, motives, purposes, or causal links that are not explicitly stated."
    )
    if branch_context.get("secondary_question_contexts"):
        base += (
            " Secondary question contexts are provenance only. "
            "Do not restate another branch's explanatory backbone as a fresh claim in this batch unless the evidence here explicitly carries that proposition."
        )
    base += (
        " Bad shape: 'a problem existed, so authorities introduced a fix' in one claim. "
        "Better: split the precondition from the later response. "
        "Bad shape: 'a mechanism requires backing and later prices self-correct' in one claim. "
        "Better: split the operating constraint from the later adjustment mechanism when they could become different beats downstream. "
        "If you are unsure, prefer the narrower or split version over a compressed mini-scene claim."
    )
    branch_intent = str(branch_context.get("branch_intent", "")).strip().lower()
    if branch_intent == "debate":
        return (
            base
            + " Because this branch is a debate, prefer claims that preserve explicit arguments, tradeoffs, criticisms, or attributed pro/con positions. "
            + "Do not turn generic background facts into synthetic argumentative claims. "
            + "Preserve one argumentative position as one claim when possible; do not split it into multiple narrow claims unless the sub-points matter independently."
        )
    if branch_intent == "compare":
        return (
            base
            + " Because this branch is a comparison, preserve explicit differences, alternatives, and side-by-side contrasts. "
            + "Avoid flattening contrastive evidence into generic descriptive facts. "
            + "Prefer one meaningful contrast as one claim when possible instead of separate claims for every compared attribute."
        )
    if branch_intent == "case_study":
        return (
            base
            + " Because this branch is a case study, prefer claims tied to concrete actors, actions, outcomes, and consequences from the episode itself. "
            + "Preserve one coherent episode or outcome as one claim when possible instead of splitting actor, action, and result into separate micro-claims."
        )
    return base


def _claim_rebuild_signature(
    *,
    statement: str,
    claim_type: str,
    support_ids: set[str],
    illustrative_ids: set[str],
) -> str:
    payload = {
        "statement": normalize_research_text(statement).strip().lower(),
        "claim_type": str(claim_type or "").strip().lower(),
        "support_ids": sorted(str(eid) for eid in support_ids),
        "illustrative_ids": sorted(str(eid) for eid in illustrative_ids),
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _select_claim_assignment(
    *,
    statement: str,
    support_ids: set[str],
    branch_id: str,
    default_question_ids: list[str],
    batch_evidence_index: dict[str, dict],
) -> tuple[str, list[str]]:
    statement_terms = {
        token
        for token in normalize_research_text(statement).lower().split()
        if len(token) > 2
    }
    candidate_scores: dict[str, dict] = {}
    branch_scores: dict[str, float] = defaultdict(float)

    for evidence_id in support_ids:
        evidence_item = batch_evidence_index.get(str(evidence_id))
        if not isinstance(evidence_item, dict):
            continue
        lineage = evidence_item.get("lineage", {})
        for context in lineage.get("question_contexts", []):
            if not isinstance(context, dict):
                continue
            question_id = str(context.get("question_id", "")).strip()
            context_branch_id = str(context.get("branch_id", "")).strip()
            if not question_id:
                continue
            question_terms = {
                token
                for token in normalize_research_text(str(context.get("question", ""))).lower().split()
                if len(token) > 2
            }
            overlap = len(statement_terms & question_terms) / max(len(question_terms), 1) if question_terms else 0.0
            direct_bonus = 0.4 if str(context.get("direct_or_contextual", "")).strip().lower() == "direct" else 0.0
            branch_bonus = 0.2 if context_branch_id == branch_id else 0.0
            score = overlap + direct_bonus + branch_bonus
            current = candidate_scores.get(question_id)
            if current is None or score > current["score"]:
                candidate_scores[question_id] = {
                    "score": score,
                    "branch_id": context_branch_id or branch_id,
                }
            branch_scores[context_branch_id or branch_id] += score

    if candidate_scores:
        ranked_question_ids = [
            question_id
            for question_id, _ in sorted(
                candidate_scores.items(),
                key=lambda item: (item[1]["score"], item[0]),
                reverse=True,
            )
        ]
        selected_branch_id = candidate_scores[ranked_question_ids[0]]["branch_id"] or branch_id
        return selected_branch_id, ranked_question_ids[:3]

    if branch_scores:
        selected_branch_id = max(branch_scores.items(), key=lambda item: item[1])[0]
        return selected_branch_id or branch_id, default_question_ids

    return branch_id, default_question_ids


async def _persist_claim_batch(
    *,
    db,
    project_id: str,
    claims_data: list[dict],
    batch_ids: set[str],
    branch_id: str,
    branch_question_ids: list[str],
    batch_evidence_index: dict[str, dict],
    previous_rebuild_lookup: dict[str, list[dict]],
    run_id: str,
    all_claim_ids: list[str],
) -> tuple[list[dict], dict[str, int]]:
    saved_claims_for_batch: list[dict] = []
    persist_stats = {
        "skipped_no_support_count": 0,
        "skipped_empty_statement_count": 0,
        "coerced_claim_type_count": 0,
        "reused_verification_state_count": 0,
    }
    try:
        for cd in claims_data:
            if not isinstance(cd, dict):
                continue
            statement = cd.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                persist_stats["skipped_empty_statement_count"] += 1
                continue

            support_ids = {
                evidence_id
                for evidence_id in cd.get("supporting_evidence_ids", [])
                if isinstance(evidence_id, str) and evidence_id in batch_ids
            }
            illustrative_ids = {
                evidence_id
                for evidence_id in cd.get("illustrative_evidence_ids", [])
                if isinstance(evidence_id, str) and evidence_id in batch_ids
            }
            if not support_ids:
                persist_stats["skipped_no_support_count"] += 1
                continue

            claim_type, was_coerced = normalize_claim_type(cd.get("claim_type", "fact"), cd.get("attribution"))
            if was_coerced:
                persist_stats["coerced_claim_type_count"] += 1

            selected_branch_id, selected_question_ids = _select_claim_assignment(
                statement=statement,
                support_ids=support_ids,
                branch_id=branch_id,
                default_question_ids=branch_question_ids,
                batch_evidence_index=batch_evidence_index,
            )
            rebuild_signature = _claim_rebuild_signature(
                statement=statement,
                claim_type=claim_type,
                support_ids=support_ids,
                illustrative_ids=illustrative_ids,
            )
            previous_rebuild_candidates = previous_rebuild_lookup.get(rebuild_signature, [])
            previous_rebuild_state = previous_rebuild_candidates.pop(0) if previous_rebuild_candidates else None

            claim = Claim(
                id=uuid.uuid4(),
                project_id=uuid.UUID(project_id),
                claim_type=claim_type,
                statement=statement,
                attribution=cd.get("attribution"),
                notes=encode_claim_notes_metadata(
                    branch_id=selected_branch_id,
                    question_ids=selected_question_ids,
                ),
            )
            if previous_rebuild_state and not previous_rebuild_state.get("was_gap_claim"):
                claim.verification_level = previous_rebuild_state.get("verification_level", claim.verification_level)
                claim.verification_fingerprint = previous_rebuild_state.get("verification_fingerprint")
                persist_stats["reused_verification_state_count"] += 1
            db.add(claim)
            await db.flush()

            for evidence_id in sorted(support_ids | illustrative_ids):
                try:
                    db.add(
                        ClaimEvidence(
                            claim_id=claim.id,
                            evidence_id=uuid.UUID(evidence_id),
                            supports_fact=evidence_id in support_ids,
                            contradicts_fact=False,
                            illustrative=evidence_id in illustrative_ids,
                        )
                    )
                except ValueError:
                    logger.warning("Claim builder produced invalid evidence UUID: %s", evidence_id)

            await db.flush()
            claim_id = str(claim.id)
            all_claim_ids.append(claim_id)
            saved_claims_for_batch.append(
                {
                    "claim_id": claim_id,
                    "claim_type": claim.claim_type,
                    "statement": claim.statement,
                    "statement_preview": _clip_text(claim.statement, limit=240),
                    "attribution": claim.attribution,
                    "supporting_evidence_ids": sorted(support_ids),
                    "illustrative_evidence_ids": sorted(illustrative_ids),
                }
            )
            publish_event(run_id, claim_created(claim.claim_type, claim.statement[:100]))
    except Exception:
        logger.exception("Claim persistence failed")
        raise
    return saved_claims_for_batch, persist_stats


def _safe_filename_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    token = token.strip("-_")
    return token or "unknown"


def _resolve_dump_dir(raw_dir: str) -> Path:
    path = Path(raw_dir).expanduser()
    if path.is_absolute():
        return path
    return _REPO_ROOT / path


def _clip_text(value: str, limit: int = 220) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _summarize_claims_payload(claims_data: object, limit: int = 15) -> list[dict]:
    if not isinstance(claims_data, list):
        return []

    summarized: list[dict] = []
    for item in claims_data[:limit]:
        if not isinstance(item, dict):
            continue
        statement = item.get("statement")
        summarized.append(
            {
                "claim_type": item.get("claim_type"),
                "statement": statement,
                "statement_preview": _clip_text(statement, limit=240) if isinstance(statement, str) else "",
                "supporting_evidence_ids": item.get("supporting_evidence_ids", []),
                "illustrative_evidence_ids": item.get("illustrative_evidence_ids", []),
            }
        )
    return summarized


def _dump_claim_builder_batch_debug(
    *,
    run_id: str,
    batch_number: int,
    status: str,
    input_payload: dict,
    llm_raw_output: str,
    parsed_claims: object,
    saved_claims: list[dict],
    persist_stats: dict,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return

    try:
        target_dir = _resolve_dump_dir(dump_dir) / "claims"
        target_dir.mkdir(parents=True, exist_ok=True)

        payload_hash = hashlib.sha1(
            json.dumps(input_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:10]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = (
            f"{ts}_run-{_safe_filename_token(run_id)}_"
            f"batch-{batch_number:03d}_{_safe_filename_token(status)}_{payload_hash}.json"
        )

        payload = {
            "run_id": run_id,
            "batch_number": batch_number,
            "status": status,
            "input": {
                "build_mode": input_payload.get("build_mode"),
                "targeted_branch_ids": input_payload.get("targeted_branch_ids", []),
                "branch_id": input_payload.get("branch_id"),
                "branch_context": input_payload.get("branch_context"),
                "target_language": input_payload.get("target_language"),
                "instructions": input_payload.get("instructions"),
                "evidence_count": len(input_payload.get("evidence_spans", [])),
                "evidence_spans": input_payload.get("evidence_spans", []),
            },
            "llm_output": {
                "raw_chars": len(llm_raw_output or ""),
                "raw": llm_raw_output,
                "parsed_claims_type": type(parsed_claims).__name__,
                "parsed_claims_count": len(parsed_claims) if isinstance(parsed_claims, list) else None,
                "parsed_claims": parsed_claims,
                "parsed_claims_sample": _summarize_claims_payload(parsed_claims),
            },
            "persisted": {
                "saved_claim_count": len(saved_claims),
                "saved_claims": saved_claims,
                "persist_stats": persist_stats,
            },
        }

        (target_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        logger.warning(
            "Failed to write claim builder debug dump (run_id=%s, batch=%s, status=%s)",
            run_id,
            batch_number,
            status,
            exc_info=True,
        )
