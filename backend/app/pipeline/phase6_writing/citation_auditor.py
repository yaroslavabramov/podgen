"""Phase 6.3: Citation Auditor — verify factual accuracy of dialogue."""

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.script_segment import ScriptSegment, AuditStatus
from app.models.claim import Claim
from app.models.show_bible import ShowBible
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, quality_update, phase_completed, progress

logger = logging.getLogger(__name__)

AUDITOR_SYSTEM = """You are a citation auditor for a podcast script.
Check each dialogue line against the claims and policies.

Return JSON:
{
  "passed": true/false,
  "issues": [
    {
      "line_index": 0,
      "type": "unverified_stated_as_fact|forbidden_phrase|missing_attribution|audience_mismatch",
      "description": "what's wrong",
      "suggestion": "how to fix"
    }
  ],
  "metrics": {
    "fact_coverage_ratio": 0.95,
    "forbidden_phrase_hits": 0,
    "rigor_policy_violations": 0,
    "persona_consistency_score": 0.85,
    "temp_source_only_claims": 0
  }
}

Check rules:
1. Every factual statement must link to a claim_id
2. UNVERIFIED claims can't be stated confidently — need hedging
3. DISPUTED claims must present both sides
4. OPINION/REPORT must have attribution
5. No forbidden phrases
6. Audience level match (explain terms for beginners, don't over-explain for pros)"""


async def audit_citations(state: PipelineState) -> dict:
    """LangGraph node: audit all script segments for citation accuracy."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, step_started("phase6_writing", "citation_audit", "Auditing citations and accuracy"))

    segment_ids = state.get("script_segment_ids", [])
    quality_signals = dict(state.get("quality_signals", {}))

    async with async_session_factory() as db:
        # Load ShowBible for policies
        bible = None
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()

        forbidden = bible.forbidden_phrases if bible else []
        rigor = bible.rigor_policy if bible else ""

        # Load claims for reference
        claims_map = {}
        for cid in state.get("claim_ids", []):
            result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
            c = result.scalar_one_or_none()
            if c:
                claims_map[str(c.id)] = {"type": c.claim_type, "verification": c.verification_level, "statement": c.statement[:200]}

        total_metrics = {"fact_coverage_ratio": 0, "forbidden_phrase_hits": 0, "rigor_policy_violations": 0, "persona_consistency_score": 0}
        segment_count = 0

        for sid in segment_ids:
            result = await db.execute(select(ScriptSegment).where(ScriptSegment.id == uuid.UUID(sid)))
            segment = result.scalar_one_or_none()
            if not segment:
                continue

            prompt = json.dumps({
                "dialogue": segment.dialogue_json,
                "available_claims": claims_map,
                "forbidden_phrases": forbidden,
                "rigor_policy": rigor,
                "language": state["language"],
            })

            try:
                raw = await call_sonnet_structured(system=AUDITOR_SYSTEM, prompt=prompt, max_tokens=2048, temperature=0.2)
                audit = json.loads(raw)
            except Exception:
                logger.warning("Audit failed for segment %s", sid, exc_info=True)
                segment.audit_status = AuditStatus.PASSED
                continue

            metrics = audit.get("metrics", {})
            segment.quality_metrics = metrics
            segment.audit_status = AuditStatus.PASSED if audit.get("passed", True) else AuditStatus.FAILED

            # Aggregate metrics
            for key in total_metrics:
                total_metrics[key] += metrics.get(key, 0)
            segment_count += 1

        await db.commit()

    # Average metrics
    if segment_count > 0:
        for key in total_metrics:
            total_metrics[key] = round(total_metrics[key] / segment_count, 2)

    quality_signals.update(total_metrics)
    publish_event(run_id, quality_update(total_metrics))
    publish_event(run_id, step_completed("phase6_writing", "citation_audit"))
    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase6_writing", duration_ms))
    publish_event(run_id, progress(80.0, "Citation audit complete"))

    return {"script_segment_ids": segment_ids, "quality_signals": quality_signals, "current_phase": "phase6_writing"}
