"""Phase 4.1: Claim Builder — cluster evidence into typed claims."""

import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.evidence import EvidenceSpan
from app.models.claim import Claim, ClaimEvidence
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, claim_created, progress

logger = logging.getLogger(__name__)

BUILD_CLAIMS_SYSTEM = """You are a claim builder for a fact-checked podcast pipeline.
Given evidence spans, cluster them into claims.

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
- Group related evidence into single claims
- OPINION/REPORT must have attribution
- Preserve narrative assets in illustrative_evidence_ids
- Statements should be in the podcast target language"""


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
        batch_size = 30
        for batch_start in range(0, len(evidence_list), batch_size):
            batch = evidence_list[batch_start:batch_start + batch_size]
            batch_ids = {e["id"] for e in batch}

            prompt = json.dumps({
                "evidence_spans": batch,
                "target_language": state["language"],
                "instructions": "Build claims from these evidence spans. Group related evidence.",
            }, ensure_ascii=False)

            try:
                raw = await call_sonnet_structured(
                    system=BUILD_CLAIMS_SYSTEM,
                    prompt=prompt,
                    max_tokens=4096,
                    temperature=0.4,
                )
                claims_data = parse_llm_json(raw)
            except Exception:
                logger.error("Claim building failed", exc_info=True)
                continue

            for cd in claims_data:
                claim = Claim(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(state["project_id"]),
                    claim_type=cd.get("claim_type", "fact").lower(),
                    statement=cd["statement"],
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

                publish_event(run_id, claim_created(claim.claim_type, claim.statement[:100]))

        await db.commit()

    publish_event(run_id, step_completed("phase4_claims", "cluster_evidence"))
    publish_event(run_id, progress(40.0, f"Created {len(all_claim_ids)} claims from evidence"))
    update_generation_progress(run_id, "phase4_claims", "claim_builder_done", 40.0)

    return {"claim_ids": all_claim_ids, "current_phase": "phase4_claims"}
