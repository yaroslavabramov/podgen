"""Phase 6.2: Dialogue audit + conditional polish."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.models.script_segment import AuditStatus, ScriptSegment
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.phase6_writing.dialogue_utils import (
    dedupe_preserve_order as _dedupe_preserve_order,
    extract_dialogue_claim_ids as _extract_dialogue_claim_ids,
    extract_dialogue_texts as _extract_dialogue_texts,
    load_manifest_scene_metadata_lookup as _load_manifest_scene_metadata_lookup,
    normalize_dialogue_output as _normalize_dialogue_output,
    segment_audit_finding as _segment_audit_finding,
    set_segment_audit_finding as _set_segment_audit_finding,
)
from app.pipeline.state import PipelineState
from app.realtime.events import step_completed, step_started, writer_revolt
from app.realtime.publisher import publish_event
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

ISSUE_SEVERITIES = {"low", "medium", "high"}
REPAIR_SCOPES = {"scene_polish", "scene_rewrite", "act_warning"}

EVAL_SYSTEM = """You are a podcast dialogue quality auditor.
Score the scene for engagement quality (0.0 to 1.0) and return structured findings.

Return JSON:
{
  "score": 0.75,
  "passed": true,
  "issues": [
    {
      "type": "dry_facts|persona_mismatch|repetitive|no_examples|rigid_turn_taking|repetitive_agreement_shape|repetitive_challenge_shape|scene_opening_echo|teacherly_exposition|boundary_restatement",
      "severity": "low|medium|high",
      "scope": "scene_polish|scene_rewrite|act_warning",
      "description": "what is wrong",
      "suggestion": "how to improve it"
    }
  ]
}

Scoring guide:
- 0.9-1.0: Excellent — compelling, varied, natural
- 0.7-0.9: Good — solid but could use more personality
- 0.5-0.7: Mediocre — mechanically correct but too repetitive or too lecture-shaped
- 0.0-0.5: Poor — dry, robotic, off-persona, or structurally clumsy

Look for:
- whether the scene opens in a way that matches its `scene_job` instead of drifting back into the previous beat
- scene-to-scene echo where the new scene opens by repeating the previous scene's punchline
- boundary restatement where the opening restates what the boundary context already established
- rigid turn-taking that makes the exchange feel mechanically alternating
- repetitive agreement shapes where explicit confirmations dominate the rhythm
- repetitive challenge shapes where objections or questions sound too similar
- teacherly exposition where dialogue explains correctly but without enough conversational movement

Severity guide:
- `low`: noticeable but acceptable; no rewrite needed now
- `medium`: degrades the scene and should be repaired with `scene_polish`
- `high`: structurally bad enough that a full scene rewrite may be justified

Scope guide:
- `scene_polish`: rewrite the scene for flow and naturalness while preserving facts
- `scene_rewrite`: scene is structurally wrong enough to send back through screenwriter
- `act_warning`: this scene is acceptable locally, but points to a larger act-level problem"""

POLISH_SYSTEM = """You are polishing a podcast dialogue scene for naturalness and flow.

Return JSON:
{
  "dialogue": [
    {"speaker": "A", "text": "line", "claim_ids": ["id"]},
    {"speaker": "B", "text": "line", "claim_ids": []}
  ]
}

Rules:
- Preserve the factual meaning, chronology, and required claim coverage.
- Preserve attribution and institutional details already present.
- Do not add new facts, names, dates, figures, laws, or causal claims.
- Keep the scene's `scene_job` intact. Improve delivery without changing the scene's intended move.
- Use the audit issues to improve scene flow, boundary handling, and conversational variety.
- If the boundary context already landed a punchline, do not reopen by paraphrasing it.
- Rewrite the whole scene if needed for rhythm, but keep the same scene job and content function.
- Keep only speakers `A` and `B`.
- Output only JSON."""


async def evaluate_and_revolt(state: PipelineState) -> dict:
    """LangGraph node: audit dialogue quality, polish scenes, fallback to rewrite when needed."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase6_writing", "engagement_eval", "Auditing and polishing dialogue"))

    segment_ids = _dedupe_preserve_order(state.get("script_segment_ids", []))
    iteration_counts = dict(state.get("iteration_counts", {}))
    revolt_count = iteration_counts.get("writer_planner_loops", 0)
    quality_signals = dict(state.get("quality_signals", {}))

    if revolt_count >= settings.max_writer_revolt_per_act:
        logger.info("Writer revolt governor hit: %d revolts", revolt_count)
        publish_event(run_id, step_completed("phase6_writing", "engagement_eval"))
        return {
            "script_segment_ids": segment_ids,
            "iteration_counts": iteration_counts,
            "quality_signals": quality_signals,
        }

    flagged_scenes = 0
    polished_scenes = 0
    rewrite_requested_scenes = 0

    async with async_session_factory() as db:
        segments = await _load_segments_for_audit(db, segment_ids)
        scene_job_lookup = await _load_scene_job_lookup(db, state.get("manifest_id"))

        for idx, segment in enumerate(segments):
            if segment.revision_count >= settings.max_writer_revolt_per_scene:
                continue

            dialogue_text = json.dumps(segment.dialogue_json, ensure_ascii=False)
            if len(dialogue_text) < 50:
                continue

            previous_segment = segments[idx - 1] if idx > 0 else None
            boundary_context = _build_boundary_context(previous_segment)
            scene_job = scene_job_lookup.get((segment.act_number, segment.scene_number), "")
            prompt_payload = _build_dialogue_audit_prompt_payload(
                segment=segment,
                boundary_context=boundary_context,
                scene_job=scene_job,
            )

            try:
                audit = await _run_dialogue_audit(prompt_payload)
            except Exception as exc:
                logger.warning(
                    "Dialogue audit failed for act %s scene %s",
                    segment.act_number,
                    segment.scene_number,
                    exc_info=True,
                )
                _dump_dialogue_audit_debug(
                    run_id=run_id,
                    act_number=segment.act_number,
                    scene_number=segment.scene_number,
                    status="llm_or_parse_failed",
                    prompt_payload=prompt_payload,
                    audit=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            score = audit.get("score", 0.8)
            issues = audit.get("issues", [])
            passed = bool(audit.get("passed", not issues))
            segment.engagement_score = score
            flagged_scenes += 1 if issues else 0
            _set_segment_audit_finding(
                segment,
                "dialogue",
                {
                    "score": score,
                    "passed": passed,
                    "issues": issues,
                    "boundary_context": boundary_context,
                    "polish_applied": False,
                    "rewrite_requested": False,
                },
            )
            _dump_dialogue_audit_debug(
                run_id=run_id,
                act_number=segment.act_number,
                scene_number=segment.scene_number,
                status="ok",
                prompt_payload=prompt_payload,
                audit={"score": score, "passed": passed, "issues": issues},
                error=None,
            )

            if not issues:
                continue

            if (
                _should_polish_dialogue(issues)
                and revolt_count < settings.max_writer_revolt_per_act
            ):
                publish_event(
                    run_id,
                    writer_revolt(segment.act_number, segment.scene_number, issues[0]["type"]),
                )
                accepted = await _attempt_dialogue_polish(
                    db=db,
                    run_id=run_id,
                    segment=segment,
                    boundary_context=boundary_context,
                    scene_job=scene_job,
                    issues=issues,
                )
                if accepted:
                    polished_scenes += 1
                    revolt_count += 1
                    continue

            if (
                _should_request_scene_rewrite(issues)
                and revolt_count < settings.max_writer_revolt_per_act
            ):
                publish_event(
                    run_id,
                    writer_revolt(segment.act_number, segment.scene_number, issues[0]["type"]),
                )
                segment.audit_status = AuditStatus.REVISED
                segment.revision_count += 1
                rewrite_requested_scenes += 1
                revolt_count += 1
                _set_segment_audit_finding(
                    segment,
                    "dialogue",
                    {
                        **_segment_audit_finding(segment, "dialogue"),
                        "rewrite_requested": True,
                    },
                )

        await db.commit()

    iteration_counts["writer_planner_loops"] = revolt_count
    quality_signals["dialogue_flagged_scenes"] = flagged_scenes
    quality_signals["dialogue_polished_scenes"] = polished_scenes
    quality_signals["dialogue_rewrite_requested_scenes"] = rewrite_requested_scenes
    quality_signals["revolt_triggered"] = rewrite_requested_scenes > 0

    publish_event(run_id, step_completed("phase6_writing", "engagement_eval"))
    return {
        "script_segment_ids": segment_ids,
        "iteration_counts": iteration_counts,
        "quality_signals": quality_signals,
    }


async def _load_segments_for_audit(db, segment_ids: list[str]) -> list[ScriptSegment]:
    segments: list[ScriptSegment] = []
    for sid in segment_ids:
        try:
            segment_uuid = uuid.UUID(sid)
        except (TypeError, ValueError):
            continue
        result = await db.execute(select(ScriptSegment).where(ScriptSegment.id == segment_uuid))
        segment = result.scalar_one_or_none()
        if segment is None:
            continue
        segments.append(segment)
    segments.sort(key=lambda item: (item.act_number, item.scene_number))
    return segments


async def _load_scene_job_lookup(db, manifest_id: str | None) -> dict[tuple[int, int], str]:
    metadata_lookup = await _load_manifest_scene_metadata_lookup(db, manifest_id)
    lookup: dict[tuple[int, int], str] = {}
    for key, metadata in metadata_lookup.items():
        if not isinstance(metadata, dict):
            continue
        scene_job = metadata.get("scene_job")
        if isinstance(scene_job, str) and scene_job.strip():
            lookup[key] = scene_job.strip()
    return lookup


async def _run_dialogue_audit(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    raw = await call_haiku_structured(
        system=EVAL_SYSTEM,
        prompt=json.dumps(prompt_payload, ensure_ascii=False),
        max_tokens=1024,
        temperature=0.2,
    )
    evaluation = parse_llm_json(raw)
    score = evaluation.get("score", 0.8)
    try:
        score_value = float(score)
    except (TypeError, ValueError):
        score_value = 0.8
    score_value = max(0.0, min(score_value, 1.0))
    issues = _normalize_dialogue_issues(evaluation.get("issues"))
    passed = bool(evaluation.get("passed", not issues))
    return {"score": score_value, "passed": passed, "issues": issues}


async def _attempt_dialogue_polish(
    *,
    db,
    run_id: str,
    segment: ScriptSegment,
    boundary_context: dict[str, Any] | None,
    scene_job: str,
    issues: list[dict[str, Any]],
) -> bool:
    original_dialogue = segment.dialogue_json
    original_claim_ids = set(_extract_dialogue_claim_ids(original_dialogue))
    prompt_payload = _build_dialogue_polish_prompt_payload(
        segment=segment,
        scene_job=scene_job,
        boundary_context=boundary_context,
        issues=issues,
    )

    try:
        raw = await call_sonnet_structured(
            system=POLISH_SYSTEM,
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            max_tokens=2048,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        polished_dialogue = _normalize_dialogue_output(parsed.get("dialogue"))
    except Exception as exc:
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="llm_or_parse_failed",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=None,
            decision="rejected",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False

    if not polished_dialogue:
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="empty_dialogue",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=polished_dialogue,
            decision="rejected",
            error="polish produced no usable dialogue",
        )
        return False

    polished_claim_ids = set(_extract_dialogue_claim_ids(polished_dialogue))
    if original_claim_ids and not original_claim_ids.issubset(polished_claim_ids):
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="claim_coverage_regressed",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=polished_dialogue,
            decision="rejected",
            error="polish dropped required claim ids",
        )
        return False

    segment.dialogue_json = polished_dialogue
    segment.revision_count += 1
    _set_segment_audit_finding(
        segment,
        "dialogue",
        {
            **_segment_audit_finding(segment, "dialogue"),
            "polish_applied": True,
            "rewrite_requested": False,
        },
    )
    await db.flush()
    _dump_dialogue_polish_debug(
        run_id=run_id,
        act_number=segment.act_number,
        scene_number=segment.scene_number,
        status="accepted",
        payload=prompt_payload,
        before_dialogue=original_dialogue,
        after_dialogue=polished_dialogue,
        decision="accepted",
        error=None,
    )
    return True


def _build_dialogue_polish_prompt_payload(
    *,
    segment: ScriptSegment,
    scene_job: str,
    boundary_context: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "act_number": segment.act_number,
        "scene_number": segment.scene_number,
        "scene_job": scene_job,
        "boundary_context": boundary_context,
        "issues": issues,
        "dialogue": segment.dialogue_json,
    }


def _build_dialogue_audit_prompt_payload(
    *,
    segment: ScriptSegment,
    boundary_context: dict[str, Any] | None,
    scene_job: str,
) -> dict[str, Any]:
    return {
        "act_number": segment.act_number,
        "scene_number": segment.scene_number,
        "scene_job": scene_job,
        "boundary_context": boundary_context,
        "dialogue": segment.dialogue_json,
    }


def _build_boundary_context(previous_segment: ScriptSegment | None) -> dict[str, Any] | None:
    if previous_segment is None:
        return None

    texts = _extract_dialogue_texts(previous_segment.dialogue_json)
    tail_lines = texts[-2:] if len(texts) > 1 else texts[-1:]
    if not tail_lines:
        return None

    return {
        "act_number": previous_segment.act_number,
        "scene_number": previous_segment.scene_number,
        "last_lines": tail_lines,
        "last_line": tail_lines[-1],
    }


def _normalize_dialogue_issues(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        issue_type = item.get("type")
        severity = item.get("severity")
        scope = item.get("scope")
        description = item.get("description")
        suggestion = item.get("suggestion")
        if not isinstance(issue_type, str) or not issue_type.strip():
            continue
        if not isinstance(severity, str) or severity.strip().lower() not in ISSUE_SEVERITIES:
            severity = "medium"
        else:
            severity = severity.strip().lower()
        if not isinstance(scope, str) or scope.strip().lower() not in REPAIR_SCOPES:
            scope = "scene_polish" if severity in {"medium", "high"} else "act_warning"
        else:
            scope = scope.strip().lower()
        normalized.append(
            {
                "type": issue_type.strip(),
                "severity": severity,
                "scope": scope,
                "description": str(description or "").strip(),
                "suggestion": str(suggestion or "").strip(),
            }
        )
    return normalized


def _severity_rank(value: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(value, 0)


def _should_polish_dialogue(issues: list[dict[str, Any]]) -> bool:
    return any(
        issue.get("scope") == "scene_polish" and _severity_rank(str(issue.get("severity"))) >= 2
        for issue in issues
    )


def _should_request_scene_rewrite(issues: list[dict[str, Any]]) -> bool:
    return any(
        issue.get("scope") == "scene_rewrite" and _severity_rank(str(issue.get("severity"))) >= 3
        for issue in issues
    )


def _dump_dialogue_audit_debug(
    *,
    run_id: str,
    act_number: int,
    scene_number: int,
    status: str,
    prompt_payload: dict[str, Any],
    audit: dict[str, Any] | None,
    error: str | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="dialogue_audit",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"act-{act_number:02d}_scene-{scene_number:02d}_{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "act_number": act_number,
            "scene_number": scene_number,
            "status": status,
            "input": prompt_payload,
            "output": audit,
            "error": error,
        },
    )


def _dump_dialogue_polish_debug(
    *,
    run_id: str,
    act_number: int,
    scene_number: int,
    status: str,
    payload: dict[str, Any],
    before_dialogue: list[dict[str, Any]],
    after_dialogue: list[dict[str, Any]] | None,
    decision: str,
    error: str | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="dialogue_polish",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"act-{act_number:02d}_scene-{scene_number:02d}_{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "act_number": act_number,
            "scene_number": scene_number,
            "status": status,
            "decision": decision,
            "input": payload,
            "before_dialogue": before_dialogue,
            "after_dialogue": after_dialogue,
            "error": error,
        },
    )
