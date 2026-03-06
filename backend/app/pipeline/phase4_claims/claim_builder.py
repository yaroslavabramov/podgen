"""Phase 4.1: Claim Builder — cluster evidence into typed claims."""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.evidence import EvidenceSpan
from app.models.claim import Claim, ClaimEvidence
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, claim_created, progress

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[4]

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
- Preserve narrative assets in illustrative_evidence_ids.
- Use only evidence IDs that are present in the input.
- Return raw JSON only. Do not use markdown/code fences or extra prose."""


async def build_claims(state: PipelineState) -> dict:
    """LangGraph node: build claims from evidence."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase4_claims", "Building and categorizing claims"))
    update_generation_progress(run_id, "phase4_claims", "claim_builder", 35.0)

    evidence_ids = state.get("evidence_ids", [])
    if not evidence_ids:
        return {"claim_ids": [], "current_phase": "phase4_claims"}

    all_claim_ids = []

    async with async_session_factory() as db:
        # Load evidence spans
        evidence_list = []
        for eid in evidence_ids:
            result = await db.execute(select(EvidenceSpan).where(EvidenceSpan.id == uuid.UUID(eid)))
            ev = result.scalar_one_or_none()
            if ev:
                evidence_list.append({
                    "id": str(ev.id),
                    "verbatim_quote": ev.verbatim_quote[:500],
                    "asset_type": ev.asset_type,
                    "narrative_value_score": ev.narrative_value_score,
                    "reliability_hint": ev.reliability_hint,
                    "source_id": str(ev.source_id),
                })

        if not evidence_list:
            return {"claim_ids": [], "current_phase": "phase4_claims"}

        publish_event(run_id, step_started("phase4_claims", "cluster_evidence", f"Clustering {len(evidence_list)} evidence spans"))

        # Process in batches if needed
        # Keep enough context for cross-evidence clustering while staying below output limits.
        batch_size = 24
        for batch_start in range(0, len(evidence_list), batch_size):
            batch_number = (batch_start // batch_size) + 1
            batch = evidence_list[batch_start:batch_start + batch_size]
            batch_ids = {e["id"] for e in batch}
            payload = {
                "evidence_spans": batch,
                "target_language": state["language"],
                "instructions": (
                    "Build atomic, non-duplicative claims from these evidence spans. "
                    "Do not introduce new facts. "
                    "If a statement would contain multiple independent facts, split it into separate claims."
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

            saved_claims_for_batch: list[dict] = []
            try:
                for cd in claims_data:
                    if not isinstance(cd, dict):
                        continue
                    statement = cd.get("statement")
                    if not isinstance(statement, str) or not statement.strip():
                        continue

                    claim = Claim(
                        id=uuid.uuid4(),
                        project_id=uuid.UUID(state["project_id"]),
                        claim_type=cd.get("claim_type", "fact").lower(),
                        statement=statement,
                        attribution=cd.get("attribution"),
                    )
                    db.add(claim)
                    await db.flush()

                    # Create ClaimEvidence links.
                    # PK is (claim_id, evidence_id), so a single evidence cannot have
                    # both "supporting" and "illustrative" links for one claim.
                    # If both are present, keep the stronger "supporting" relation.
                    merged_relations: dict[str, str] = {}

                    for se_id in cd.get("supporting_evidence_ids", []):
                        if isinstance(se_id, str) and se_id in batch_ids:
                            merged_relations[se_id] = "supporting"

                    for ie_id in cd.get("illustrative_evidence_ids", []):
                        if (
                            isinstance(ie_id, str)
                            and ie_id in batch_ids
                            and ie_id not in merged_relations
                        ):
                            merged_relations[ie_id] = "illustrative"

                    for evidence_id, relation in merged_relations.items():
                        try:
                            db.add(
                                ClaimEvidence(
                                    claim_id=claim.id,
                                    evidence_id=uuid.UUID(evidence_id),
                                    relation=relation,
                                )
                            )
                        except ValueError:
                            logger.warning(
                                "Claim builder produced invalid evidence UUID: %s",
                                evidence_id,
                            )

                    await db.flush()
                    all_claim_ids.append(str(claim.id))
                    saved_claims_for_batch.append(
                        {
                            "claim_id": str(claim.id),
                            "claim_type": claim.claim_type,
                            "statement": claim.statement,
                            "statement_preview": _clip_text(claim.statement, limit=240),
                            "attribution": claim.attribution,
                            "supporting_evidence_ids": [
                                evidence_id
                                for evidence_id, relation in merged_relations.items()
                                if relation == "supporting"
                            ],
                            "illustrative_evidence_ids": [
                                evidence_id
                                for evidence_id, relation in merged_relations.items()
                                if relation == "illustrative"
                            ],
                        }
                    )

                    publish_event(run_id, claim_created(claim.claim_type, claim.statement[:100]))
            except Exception:
                _dump_claim_builder_batch_debug(
                    run_id=run_id,
                    batch_number=batch_number,
                    status="claim_persist_failed",
                    input_payload=payload,
                    llm_raw_output=raw,
                    parsed_claims=claims_data,
                    saved_claims=saved_claims_for_batch,
                )
                raise

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
