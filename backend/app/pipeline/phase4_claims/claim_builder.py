"""Phase 4.1: Claim Builder — cluster evidence into typed claims."""

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.models.evidence import EvidenceProvenance, EvidenceSpan
from app.models.claim import Claim, ClaimEvidence
from app.models.source import Source
from app.pipeline.research_context import build_question_context_map
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, claim_created, progress

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[4]
CLAIM_BATCH_CHAR_BUDGET = 14000

BUILD_CLAIMS_SYSTEM = """You are a claim builder for a fact-checked podcast pipeline.
Given evidence spans, produce a high-precision set of claims.

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
- One claim = one atomic proposition. Do not combine independent facts in one statement.
- Do not add facts, entities, numbers, dates, motives, or causal links that are not explicitly present in evidence.
- FACT must be directly supported by supporting_evidence_ids.
- OPINION/REPORT must have attribution with at least "who" when available.
- Deduplicate within this batch: if two candidates are semantic paraphrases of the same proposition, keep one claim and merge evidence IDs.
- If one evidence span supports multiple independent propositions, emit multiple claims.
- Prefer short statements (single sentence) in the target language; keep each statement concise.
- CASE, ANECDOTE, and EXPERT_QUOTE default to illustrative evidence unless they explicitly carry an atomic fact.
- The same evidence may appear in both supporting_evidence_ids and illustrative_evidence_ids.
- Use source metadata, branch/question intent, lineage, direct_or_contextual, and contextual_asset_types to avoid mixing different research branches into one claim.
- Use only evidence IDs that are present in the input.
- Return raw JSON only. Do not use markdown/code fences or extra prose."""


async def build_claims(state: PipelineState) -> dict:
    """LangGraph node: build claims from evidence."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase4_claims", "Building and categorizing claims"))
    update_generation_progress(run_id, "phase4_claims", "claim_builder", 35.0)

    evidence_ids = list(dict.fromkeys(state.get("evidence_ids", [])))
    if not evidence_ids:
        return {"claim_ids": [], "current_phase": "phase4_claims"}

    all_claim_ids: list[str] = []

    async with async_session_factory() as db:
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
        if not evidence_index:
            return {"claim_ids": [], "current_phase": "phase4_claims"}

        publish_event(
            run_id,
            step_started(
                "phase4_claims",
                "cluster_evidence",
                f"Clustering {len(evidence_index)} evidence spans across {len(branch_batches)} branch groups",
            ),
        )

        batch_number = 0
        for branch_id, evidence_group in branch_batches:
            for batch in _split_evidence_batch(evidence_group):
                batch_number += 1
                batch_ids = {e["id"] for e in batch}
                branch_context = _summarize_branch_context(branch_id, batch, question_context_map)
                payload = {
                    "branch_id": branch_id,
                    "branch_context": branch_context,
                    "evidence_spans": batch,
                    "target_language": state["language"],
                    "instructions": (
                        "Build atomic, non-duplicative claims from these evidence spans. "
                        "Do not introduce new facts. "
                        "Preserve narrative evidence by using illustrative_evidence_ids, "
                        "and use supporting_evidence_ids only when the quote directly supports the claim. "
                        "Prefer direct evidence for factual backbone; use contextual evidence as framing or illustration unless it explicitly carries the proposition."
                    ),
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
                    claims_data = parse_llm_json(raw)
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
                    )
                    continue

                saved_claims_for_batch = await _persist_claim_batch(
                    db=db,
                    project_id=state["project_id"],
                    claims_data=claims_data,
                    batch_ids=batch_ids,
                    run_id=run_id,
                    all_claim_ids=all_claim_ids,
                )
                _dump_claim_builder_batch_debug(
                    run_id=run_id,
                    batch_number=batch_number,
                    status="ok",
                    input_payload=payload,
                    llm_raw_output=raw,
                    parsed_claims=claims_data,
                    saved_claims=saved_claims_for_batch,
                )

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "cluster_evidence"))
    publish_event(run_id, progress(40.0, f"Created {len(all_claim_ids)} claims from evidence"))
    update_generation_progress(run_id, "phase4_claims", "claim_builder_done", 40.0)

    return {"claim_ids": all_claim_ids, "current_phase": "phase4_claims"}


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
            "verbatim_quote": evidence.verbatim_quote[:700],
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
    question_contexts: dict[str, dict] = {}
    intents: set[str] = set()
    for item in batch:
        lineage = item.get("lineage", {})
        for context in lineage.get("question_contexts", []):
            if not isinstance(context, dict):
                continue
            question_id = str(context.get("question_id", "")).strip()
            if not question_id:
                continue
            question_contexts[question_id] = context
            intent = context.get("intent")
            if isinstance(intent, str) and intent:
                intents.add(intent)
    return {
        "branch_id": branch_id,
        "branch_question": branch_question.get("question", ""),
        "branch_intent": branch_question.get("intent", ""),
        "question_ids": sorted(question_contexts),
        "question_contexts": [question_contexts[qid] for qid in sorted(question_contexts)],
        "intents": sorted(intents),
    }


async def _persist_claim_batch(
    *,
    db,
    project_id: str,
    claims_data: list[dict],
    batch_ids: set[str],
    run_id: str,
    all_claim_ids: list[str],
) -> list[dict]:
    saved_claims_for_batch: list[dict] = []
    try:
        for cd in claims_data:
            if not isinstance(cd, dict):
                continue
            statement = cd.get("statement")
            if not isinstance(statement, str) or not statement.strip():
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
                continue

            claim = Claim(
                id=uuid.uuid4(),
                project_id=uuid.UUID(project_id),
                claim_type=str(cd.get("claim_type", "fact")).lower(),
                statement=statement,
                attribution=cd.get("attribution"),
            )
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
    return saved_claims_for_batch


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
