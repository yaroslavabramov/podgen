"""Phase 6.2: Writer Revolt — evaluate engagement, request improvements."""

import json
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.script_segment import ScriptSegment, AuditStatus
from app.pipeline.state import PipelineState
from app.services.llm import call_haiku_structured
from app.pipeline.llm_json import parse_llm_json
from app.config import settings
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, writer_revolt

logger = logging.getLogger(__name__)

EVAL_SYSTEM = """You are a podcast engagement evaluator.
Score the dialogue for engagement quality (0.0 to 1.0).

Return JSON:
{
  "score": 0.75,
  "weak_points": [
    {"scene": 1, "issue": "dry_facts|persona_mismatch|repetitive|no_examples", "suggestion": "..."}
  ]
}

Scoring guide:
- 0.9-1.0: Excellent — compelling, varied, natural
- 0.7-0.9: Good — solid but could use more personality
- 0.5-0.7: Mediocre — needs more stories, examples, or character
- 0.0-0.5: Poor — dry, robotic, or off-persona"""


async def evaluate_and_revolt(state: PipelineState) -> dict:
    """LangGraph node: evaluate engagement, trigger rewrites if needed."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase6_writing", "engagement_eval", "Evaluating engagement quality"))

    segment_ids = _dedupe_preserve_order(state.get("script_segment_ids", []))
    iteration_counts = dict(state.get("iteration_counts", {}))
    revolt_count = iteration_counts.get("writer_planner_loops", 0)

    if revolt_count >= settings.max_writer_revolt_per_act:
        logger.info("Writer revolt governor hit: %d revolts", revolt_count)
        publish_event(run_id, step_completed("phase6_writing", "engagement_eval"))
        return {"script_segment_ids": segment_ids, "iteration_counts": iteration_counts}

    async with async_session_factory() as db:
        for sid in segment_ids:
            result = await db.execute(select(ScriptSegment).where(ScriptSegment.id == uuid.UUID(sid)))
            segment = result.scalar_one_or_none()
            if not segment or segment.revision_count >= settings.max_writer_revolt_per_scene:
                continue

            dialogue_text = json.dumps(segment.dialogue_json)
            if len(dialogue_text) < 50:
                continue

            try:
                raw = await call_haiku_structured(
                    system=EVAL_SYSTEM,
                    prompt=json.dumps({
                        "act": segment.act_number,
                        "scene": segment.scene_number,
                        "dialogue": segment.dialogue_json,
                    }, ensure_ascii=False),
                    max_tokens=1024,
                    temperature=0.2,
                )
                evaluation = parse_llm_json(raw)
            except Exception:
                continue

            score = evaluation.get("score", 0.8)
            segment.engagement_score = score

            if score < 0.6 and revolt_count < settings.max_writer_revolt_per_act:
                weak_points = evaluation.get("weak_points", [])
                if weak_points:
                    reason = weak_points[0].get("issue", "low_engagement")
                    publish_event(run_id, writer_revolt(segment.act_number, segment.scene_number, reason))

                    # Mark for revision (actual rewrite happens in screenwriter re-run if needed)
                    segment.audit_status = AuditStatus.REVISED
                    segment.revision_count += 1
                    revolt_count += 1

        await db.commit()

    iteration_counts["writer_planner_loops"] = revolt_count
    publish_event(run_id, step_completed("phase6_writing", "engagement_eval"))

    # Signal graph whether revolt triggered rewrites
    quality_signals = dict(state.get("quality_signals", {}))
    any_revolt = revolt_count > iteration_counts.get("_prev_revolt_count", 0)
    quality_signals["revolt_triggered"] = any_revolt
    iteration_counts["_prev_revolt_count"] = revolt_count

    return {
        "script_segment_ids": segment_ids,
        "iteration_counts": iteration_counts,
        "quality_signals": quality_signals,
    }


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
