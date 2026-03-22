"""Phase 6.2: Dialogue audit + conditional polish."""

from __future__ import annotations

import json
import logging
import re
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
    merge_dialogue_metadata as _merge_dialogue_metadata,
    normalize_dialogue_output as _normalize_dialogue_output,
    segment_audit_finding as _segment_audit_finding,
    set_segment_audit_finding as _set_segment_audit_finding,
)
from app.pipeline.state import PipelineState
from app.pipeline.step_control import StepExecutionStopped
from app.realtime.events import step_completed, step_started, writer_revolt
from app.realtime.publisher import publish_event
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

ISSUE_SEVERITIES = {"low", "medium", "high"}
REPAIR_SCOPES = {"scene_polish", "scene_rewrite", "act_warning"}
ECHO_ISSUE_TYPES = {"scene_opening_echo", "boundary_restatement"}
REPETITION_PRIORITY_TYPES = {
    "scene_opening_echo",
    "boundary_restatement",
    "repetitive_agreement_shape",
    "repetitive_challenge_shape",
    "rigid_turn_taking",
    "teacherly_exposition",
}

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
- `act_warning`: this scene is acceptable locally, but points to a larger act-level problem

Language contract:
- The payload includes `language`.
- Judge the scene as spoken dialogue in that language, not as translated prose.
- When describing issues or suggestions, reason about natural spoken register for that language rather than assuming English defaults."""

POLISH_SYSTEM = """You are polishing a podcast dialogue scene for naturalness and flow.

Return JSON:
{
  "dialogue": [
    {"speaker": "A", "text": "line", "claim_ids": ["id"]},
    {"speaker": "B", "text": "line", "claim_ids": []}
  ]
}

Rules:
- The payload includes `language`; keep the scene natural for that language and spoken register.
- Preserve the factual meaning, chronology, and required claim coverage.
- Preserve attribution and institutional details already present.
- Do not add new facts, names, dates, figures, laws, or causal claims.
- Keep the scene's `scene_job` intact. Improve delivery without changing the scene's intended move.
- Use the audit issues to improve scene flow, boundary handling, and conversational variety.
- The payload may include an `act_repetition_report`; if one opener, agreement, or turn-taking pattern is dominating the act, use this scene to break that pattern naturally.
- If the boundary context already landed a punchline, do not reopen by paraphrasing it.
- Rewrite the whole scene if needed for rhythm, but keep the same scene job and content function.
- Avoid translated-from-English phrasing or register drift.
- Keep only speakers `A` and `B`.
- Output only JSON."""

ACT_AUDIT_SYSTEM = """You are an act-level podcast dialogue auditor.
Review one act as a sequence of scenes and identify aggregate repetition / distribution problems.

Return JSON:
{
  "priority_scene_numbers": [2, 4],
  "distribution_findings": [
    {
      "family": "boundary_echo_cluster|confirmation_first_answers|interruption_first_questions|explanatory_resets|speaker_distribution|other",
      "severity": "low|medium|high",
      "scene_numbers": [2, 4],
      "description": "what repeated in this act and why it weakens the act"
    }
  ]
}

Rules:
- The payload includes `language`; judge the act as spoken dialogue in that language.
- Focus on act-level distribution problems, not local factual rigor.
- Use the compact scene summaries to identify repeated opener strategies, repeated confirmation/challenge habits, and speaker-distribution imbalance across scenes.
- Prioritize the smallest set of scenes whose polishing would most improve the act's variety and flow.
- `priority_scene_numbers` should contain scenes that are the best intervention points, not every scene with a local issue.
- Output only JSON."""


async def evaluate_and_revolt(state: PipelineState) -> dict:
    """LangGraph node: audit dialogue quality, polish scenes, fallback to rewrite when needed."""
    run_id = state["generation_run_id"]
    publish_event(run_id, step_started("phase6_writing", "engagement_eval", "Auditing and polishing dialogue"))

    segment_ids = _dedupe_preserve_order(state.get("script_segment_ids", []))
    iteration_counts = dict(state.get("iteration_counts", {}))
    quality_signals = dict(state.get("quality_signals", {}))
    rewrite_loop_count = _safe_int(iteration_counts.get("writer_planner_loops", 0))
    revolt_counts_by_act = _normalize_revolt_counts_by_act(iteration_counts.get("writer_revolt_counts_by_act"))

    if not segment_ids:
        publish_event(run_id, step_completed("phase6_writing", "engagement_eval"))
        return {
            "script_segment_ids": [],
            "iteration_counts": iteration_counts,
            "quality_signals": quality_signals,
        }
    flagged_scenes = 0
    polished_scenes = 0
    rewrite_requested_scenes = 0
    language = str(state.get("language") or "en").strip() or "en"
    audit_rows: list[dict[str, Any]] = []
    act_reports: dict[str, dict[str, Any]] = {}

    async with async_session_factory() as db:
        segments = await _load_segments_for_audit(db, segment_ids)
        scene_job_lookup = await _load_scene_job_lookup(db, state.get("manifest_id"))

        for idx, segment in enumerate(segments):
            dialogue_text = json.dumps(segment.dialogue_json, ensure_ascii=False)
            if len(dialogue_text) < 50:
                continue

            previous_segment = segments[idx - 1] if idx > 0 else None
            boundary_context = _build_boundary_context(previous_segment)
            scene_job = scene_job_lookup.get((segment.act_number, segment.scene_number), "")
            prompt_payload = _build_dialogue_audit_prompt_payload(
                segment=segment,
                language=language,
                boundary_context=boundary_context,
                scene_job=scene_job,
            )

            try:
                audit = await _run_dialogue_audit(prompt_payload)
            except StepExecutionStopped:
                raise
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
            _dump_dialogue_audit_debug(
                run_id=run_id,
                act_number=segment.act_number,
                scene_number=segment.scene_number,
                status="ok",
                prompt_payload=prompt_payload,
                audit={"score": score, "passed": passed, "issues": issues},
                error=None,
            )

            audit_rows.append(
                {
                    "segment": segment,
                    "score": score,
                    "passed": passed,
                    "issues": issues,
                    "boundary_context": boundary_context,
                    "scene_job": scene_job,
                    "can_intervene": segment.revision_count < settings.max_writer_revolt_per_scene,
                }
            )

        act_reports = _build_act_dialogue_reports(audit_rows)
        act_reports = await _augment_act_dialogue_reports_with_llm(
            audit_rows=audit_rows,
            act_reports=act_reports,
            language=language,
        )

        for row in audit_rows:
            segment = row["segment"]
            act_report = act_reports.get(_act_count_key(segment.act_number), {})
            _set_segment_audit_finding(
                segment,
                "dialogue",
                {
                    "score": row["score"],
                    "passed": row["passed"],
                    "issues": row["issues"],
                    "boundary_context": row["boundary_context"],
                    "polish_applied": False,
                    "rewrite_requested": False,
                    "priority_score": row.get("priority_score", 0),
                    "act_report": _dialogue_act_report_excerpt(act_report),
                    "can_intervene": row["can_intervene"],
                },
            )

        for row in _prioritize_dialogue_interventions(audit_rows, act_reports):
            segment = row["segment"]
            issues = row["issues"]
            if not issues or not row["can_intervene"]:
                continue

            act_key = _act_count_key(segment.act_number)
            act_revolt_count = revolt_counts_by_act.get(act_key, 0)
            if act_revolt_count >= settings.max_writer_revolt_per_act:
                continue

            act_report = act_reports.get(act_key, {})
            if _should_polish_dialogue(issues):
                publish_event(
                    run_id,
                    writer_revolt(segment.act_number, segment.scene_number, issues[0]["type"]),
                )
                accepted = await _attempt_dialogue_polish(
                    db=db,
                    run_id=run_id,
                    segment=segment,
                    language=language,
                    boundary_context=row["boundary_context"],
                    scene_job=row["scene_job"],
                    issues=issues,
                    act_repetition_report=_dialogue_act_report_excerpt(act_report),
                )
                if accepted:
                    polished_scenes += 1
                    revolt_counts_by_act[act_key] = act_revolt_count + 1
                    continue

            if _should_request_scene_rewrite(issues):
                publish_event(
                    run_id,
                    writer_revolt(segment.act_number, segment.scene_number, issues[0]["type"]),
                )
                segment.audit_status = AuditStatus.REVISED
                segment.revision_count += 1
                rewrite_requested_scenes += 1
                revolt_counts_by_act[act_key] = act_revolt_count + 1
                _set_segment_audit_finding(
                    segment,
                    "dialogue",
                    {
                        **_segment_audit_finding(segment, "dialogue"),
                        "rewrite_requested": True,
                    },
                )

        await db.commit()

    iteration_counts["writer_revolt_counts_by_act"] = revolt_counts_by_act
    iteration_counts["writer_planner_loops"] = rewrite_loop_count + 1 if rewrite_requested_scenes > 0 else rewrite_loop_count
    quality_signals["dialogue_flagged_scenes"] = flagged_scenes
    quality_signals["dialogue_polished_scenes"] = polished_scenes
    quality_signals["dialogue_rewrite_requested_scenes"] = rewrite_requested_scenes
    quality_signals["dialogue_revolt_counts_by_act"] = revolt_counts_by_act
    quality_signals["dialogue_act_reports"] = act_reports
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
    language: str,
    boundary_context: dict[str, Any] | None,
    scene_job: str,
    issues: list[dict[str, Any]],
    act_repetition_report: dict[str, Any] | None,
) -> bool:
    original_dialogue = segment.dialogue_json
    original_claim_ids = set(_extract_dialogue_claim_ids(original_dialogue))
    prompt_payload = _build_dialogue_polish_prompt_payload(
        segment=segment,
        language=language,
        scene_job=scene_job,
        boundary_context=boundary_context,
        issues=issues,
        act_repetition_report=act_repetition_report,
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
    except StepExecutionStopped:
        raise
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

    if _requires_material_opener_change(issues) and not _opener_changed_materially(
        original_dialogue,
        polished_dialogue,
    ):
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="opener_change_insufficient",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=polished_dialogue,
            decision="rejected",
            error="polish kept the same opener despite opener-echo issues",
        )
        return False

    merged_dialogue = _merge_dialogue_metadata(
        original_dialogue=original_dialogue,
        repaired_dialogue=polished_dialogue,
    )
    if not merged_dialogue:
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="metadata_merge_failed",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=polished_dialogue,
            decision="rejected",
            error="polish produced dialogue that could not be merged with original metadata",
        )
        return False

    segment.dialogue_json = merged_dialogue
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
        after_dialogue=merged_dialogue,
        decision="accepted",
        error=None,
    )
    return True


def _build_dialogue_polish_prompt_payload(
    *,
    segment: ScriptSegment,
    language: str,
    scene_job: str,
    boundary_context: dict[str, Any] | None,
    issues: list[dict[str, Any]],
    act_repetition_report: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "act_number": segment.act_number,
        "scene_number": segment.scene_number,
        "language": str(language or "en").strip() or "en",
        "scene_job": scene_job,
        "boundary_context": boundary_context,
        "act_repetition_report": act_repetition_report,
        "issues": issues,
        "dialogue": segment.dialogue_json,
    }


def _build_dialogue_audit_prompt_payload(
    *,
    segment: ScriptSegment,
    language: str,
    boundary_context: dict[str, Any] | None,
    scene_job: str,
) -> dict[str, Any]:
    return {
        "act_number": segment.act_number,
        "scene_number": segment.scene_number,
        "language": str(language or "en").strip() or "en",
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _act_count_key(act_number: int) -> str:
    return str(_safe_int(act_number, 0))


def _normalize_revolt_counts_by_act(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, raw_count in value.items():
        act_number = _safe_int(key, 0)
        if act_number <= 0:
            continue
        act_key = _act_count_key(act_number)
        normalized[act_key] = max(0, _safe_int(raw_count, 0))
    return normalized


def _first_valid_line(dialogue: Any) -> dict[str, Any] | None:
    if not isinstance(dialogue, list):
        return None
    for item in dialogue:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return item
    return None


def _last_valid_line(dialogue: Any) -> dict[str, Any] | None:
    if not isinstance(dialogue, list):
        return None
    for item in reversed(dialogue):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return item
    return None


def _dialogue_edge_speaker(dialogue: Any, *, edge: str) -> str:
    line = _first_valid_line(dialogue) if edge == "first" else _last_valid_line(dialogue)
    if not isinstance(line, dict):
        return ""
    speaker = line.get("speaker")
    return str(speaker or "").strip()


def _normalized_text_signature(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\W+", " ", text.casefold()).strip()


def _opener_signature(dialogue: Any) -> tuple[str, str]:
    line = _first_valid_line(dialogue)
    if not isinstance(line, dict):
        return ("", "")
    return (
        str(line.get("speaker") or "").strip(),
        _normalized_text_signature(line.get("text")),
    )


def _requires_material_opener_change(issues: list[dict[str, Any]]) -> bool:
    return any(
        issue.get("type") in ECHO_ISSUE_TYPES
        and issue.get("scope") == "scene_polish"
        and _severity_rank(str(issue.get("severity"))) >= 2
        for issue in issues
    )


def _opener_changed_materially(original_dialogue: Any, polished_dialogue: Any) -> bool:
    return _opener_signature(original_dialogue) != _opener_signature(polished_dialogue)


def _build_act_dialogue_reports(audit_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    issue_scene_numbers_by_act: dict[str, dict[str, list[int]]] = {}
    for row in audit_rows:
        segment = row["segment"]
        act_key = _act_count_key(segment.act_number)
        report = reports.setdefault(
            act_key,
            {
                "act_number": segment.act_number,
                "scene_count": 0,
                "flagged_scene_count": 0,
                "opening_speaker_counts": {},
                "closing_speaker_counts": {},
                "issue_counts": {},
            },
        )
        report["scene_count"] += 1
        if row["issues"]:
            report["flagged_scene_count"] += 1

        opening_speaker = _dialogue_edge_speaker(segment.dialogue_json, edge="first")
        closing_speaker = _dialogue_edge_speaker(segment.dialogue_json, edge="last")
        if opening_speaker:
            opening_counts = dict(report["opening_speaker_counts"])
            opening_counts[opening_speaker] = opening_counts.get(opening_speaker, 0) + 1
            report["opening_speaker_counts"] = opening_counts
        if closing_speaker:
            closing_counts = dict(report["closing_speaker_counts"])
            closing_counts[closing_speaker] = closing_counts.get(closing_speaker, 0) + 1
            report["closing_speaker_counts"] = closing_counts

        act_issue_scenes = issue_scene_numbers_by_act.setdefault(act_key, {})
        for issue in row["issues"]:
            issue_type = str(issue.get("type") or "").strip()
            if not issue_type:
                continue
            issue_counts = dict(report["issue_counts"])
            issue_counts[issue_type] = issue_counts.get(issue_type, 0) + 1
            report["issue_counts"] = issue_counts

            scene_numbers = list(act_issue_scenes.get(issue_type, []))
            if segment.scene_number not in scene_numbers:
                scene_numbers.append(segment.scene_number)
            act_issue_scenes[issue_type] = scene_numbers

    for act_key, report in reports.items():
        report["dominant_opening_speaker"] = _dominant_speaker(report["opening_speaker_counts"])
        report["dominant_closing_speaker"] = _dominant_speaker(report["closing_speaker_counts"])
        act_issue_scenes = issue_scene_numbers_by_act.get(act_key, {})
        report["repetition_families"] = [
            {
                "family": issue_type,
                "scene_numbers": sorted(scene_numbers),
                "count": len(scene_numbers),
            }
            for issue_type, scene_numbers in sorted(act_issue_scenes.items())
            if issue_type in REPETITION_PRIORITY_TYPES and scene_numbers
        ]

    for row in audit_rows:
        segment = row["segment"]
        act_key = _act_count_key(segment.act_number)
        row["priority_score"] = _dialogue_priority_score(row, reports.get(act_key, {}))

    for act_key, report in reports.items():
        report["priority_scene_numbers"] = [
            row["segment"].scene_number
            for row in sorted(
                (
                    row
                    for row in audit_rows
                    if _act_count_key(row["segment"].act_number) == act_key and row.get("priority_score", 0) > 0
                ),
                key=lambda row: (-int(row.get("priority_score", 0)), row["segment"].scene_number),
            )
        ]
    return reports


def _dialogue_priority_score(row: dict[str, Any], act_report: dict[str, Any]) -> int:
    issues = row.get("issues") or []
    if not issues:
        return 0

    score = 0
    for issue in issues:
        issue_type = str(issue.get("type") or "").strip()
        severity_rank = _severity_rank(str(issue.get("severity")))
        scope = str(issue.get("scope") or "")
        if scope == "scene_rewrite":
            score += 80 + severity_rank * 10
            continue
        if issue_type in ECHO_ISSUE_TYPES:
            score += 45 + severity_rank * 10
        elif issue_type == "repetitive_agreement_shape":
            score += 30 + severity_rank * 8
        elif issue_type == "repetitive_challenge_shape":
            score += 26 + severity_rank * 8
        elif issue_type == "rigid_turn_taking":
            score += 22 + severity_rank * 6
        elif issue_type == "teacherly_exposition":
            score += 18 + severity_rank * 6
        elif scope == "scene_polish":
            score += 14 + severity_rank * 5
        elif scope == "act_warning":
            score += 4 + severity_rank * 2

    segment = row["segment"]
    dominant_opening = str(act_report.get("dominant_opening_speaker") or "")
    opening_counts = act_report.get("opening_speaker_counts") or {}
    scene_count = _safe_int(act_report.get("scene_count"), 0)
    if (
        dominant_opening
        and _dialogue_edge_speaker(segment.dialogue_json, edge="first") == dominant_opening
        and opening_counts.get(dominant_opening, 0) > max(1, scene_count // 2)
    ):
        score += 6

    dominant_closing = str(act_report.get("dominant_closing_speaker") or "")
    closing_counts = act_report.get("closing_speaker_counts") or {}
    if (
        dominant_closing
        and _dialogue_edge_speaker(segment.dialogue_json, edge="last") == dominant_closing
        and closing_counts.get(dominant_closing, 0) > max(1, scene_count // 2)
    ):
        score += 3

    llm_priority_scene_numbers = {
        _safe_int(scene_number, 0)
        for scene_number in act_report.get("llm_priority_scene_numbers", [])
        if _safe_int(scene_number, 0) > 0
    }
    if segment.scene_number in llm_priority_scene_numbers:
        score += 12

    for finding in act_report.get("distribution_findings", []) or []:
        if not isinstance(finding, dict):
            continue
        scene_numbers = {
            _safe_int(scene_number, 0)
            for scene_number in finding.get("scene_numbers", [])
            if _safe_int(scene_number, 0) > 0
        }
        if segment.scene_number not in scene_numbers:
            continue
        score += 4 * max(1, _severity_rank(str(finding.get("severity"))))
    return score


def _dominant_speaker(counts: dict[str, int]) -> str:
    if not isinstance(counts, dict) or not counts:
        return ""
    return max(
        (
            (speaker, count)
            for speaker, count in counts.items()
            if isinstance(speaker, str) and speaker.strip()
        ),
        key=lambda item: item[1],
        default=("", 0),
    )[0]


def _prioritize_dialogue_interventions(
    audit_rows: list[dict[str, Any]],
    act_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    prioritized: list[dict[str, Any]] = []
    for act_key in sorted(act_reports.keys(), key=lambda value: _safe_int(value, 0)):
        act_rows = [
            row
            for row in audit_rows
            if _act_count_key(row["segment"].act_number) == act_key
            and row.get("priority_score", 0) > 0
            and (_should_polish_dialogue(row["issues"]) or _should_request_scene_rewrite(row["issues"]))
        ]
        prioritized.extend(
            sorted(
                act_rows,
                key=lambda row: (-int(row.get("priority_score", 0)), row["segment"].scene_number),
            )
        )
    return prioritized


def _dialogue_act_report_excerpt(act_report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(act_report, dict) or not act_report:
        return None
    return {
        "act_number": act_report.get("act_number"),
        "scene_count": act_report.get("scene_count"),
        "flagged_scene_count": act_report.get("flagged_scene_count"),
        "opening_speaker_counts": act_report.get("opening_speaker_counts"),
        "closing_speaker_counts": act_report.get("closing_speaker_counts"),
        "dominant_opening_speaker": act_report.get("dominant_opening_speaker"),
        "dominant_closing_speaker": act_report.get("dominant_closing_speaker"),
        "issue_counts": act_report.get("issue_counts"),
        "repetition_families": act_report.get("repetition_families"),
        "priority_scene_numbers": act_report.get("priority_scene_numbers"),
        "distribution_findings": act_report.get("distribution_findings"),
    }


async def _augment_act_dialogue_reports_with_llm(
    *,
    audit_rows: list[dict[str, Any]],
    act_reports: dict[str, dict[str, Any]],
    language: str,
) -> dict[str, dict[str, Any]]:
    augmented = {key: dict(value) for key, value in act_reports.items()}
    for act_key, base_report in act_reports.items():
        prompt_payload = _build_act_dialogue_audit_prompt_payload(
            act_report=base_report,
            audit_rows=[
                row
                for row in audit_rows
                if _act_count_key(row["segment"].act_number) == act_key
            ],
            language=language,
        )
        try:
            llm_report = await _run_act_dialogue_audit(prompt_payload)
        except StepExecutionStopped:
            raise
        except Exception:
            logger.warning("Act dialogue audit failed for act %s", base_report.get("act_number"), exc_info=True)
            continue
        merged = _merge_act_dialogue_report(base_report=augmented.get(act_key, {}), llm_report=llm_report)
        augmented[act_key] = merged

    for row in audit_rows:
        act_key = _act_count_key(row["segment"].act_number)
        row["priority_score"] = _dialogue_priority_score(row, augmented.get(act_key, {}))

    for act_key, report in augmented.items():
        report["priority_scene_numbers"] = [
            row["segment"].scene_number
            for row in sorted(
                (
                    row
                    for row in audit_rows
                    if _act_count_key(row["segment"].act_number) == act_key and row.get("priority_score", 0) > 0
                ),
                key=lambda row: (-int(row.get("priority_score", 0)), row["segment"].scene_number),
            )
        ]
    return augmented


def _build_act_dialogue_audit_prompt_payload(
    *,
    act_report: dict[str, Any],
    audit_rows: list[dict[str, Any]],
    language: str,
) -> dict[str, Any]:
    scene_summaries: list[dict[str, Any]] = []
    for row in sorted(audit_rows, key=lambda item: item["segment"].scene_number):
        segment = row["segment"]
        texts = _extract_dialogue_texts(segment.dialogue_json)
        scene_summaries.append(
            {
                "scene_number": segment.scene_number,
                "opening_speaker": _dialogue_edge_speaker(segment.dialogue_json, edge="first"),
                "closing_speaker": _dialogue_edge_speaker(segment.dialogue_json, edge="last"),
                "opening_lines": texts[:2],
                "closing_lines": texts[-2:] if len(texts) > 1 else texts[-1:],
                "issue_types": [str(issue.get("type") or "").strip() for issue in row.get("issues", []) if issue],
                "priority_score": row.get("priority_score", 0),
            }
        )
    return {
        "language": str(language or "en").strip() or "en",
        "act_number": act_report.get("act_number"),
        "scene_count": act_report.get("scene_count"),
        "opening_speaker_counts": act_report.get("opening_speaker_counts"),
        "closing_speaker_counts": act_report.get("closing_speaker_counts"),
        "issue_counts": act_report.get("issue_counts"),
        "heuristic_repetition_families": act_report.get("repetition_families"),
        "scenes": scene_summaries,
    }


async def _run_act_dialogue_audit(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    raw = await call_haiku_structured(
        system=ACT_AUDIT_SYSTEM,
        prompt=json.dumps(prompt_payload, ensure_ascii=False),
        max_tokens=1024,
        temperature=0.2,
    )
    parsed = parse_llm_json(raw)
    return _normalize_act_dialogue_audit(parsed)


def _normalize_act_dialogue_audit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"priority_scene_numbers": [], "distribution_findings": []}

    priority_scene_numbers: list[int] = []
    raw_scene_numbers = value.get("priority_scene_numbers")
    if isinstance(raw_scene_numbers, list):
        for item in raw_scene_numbers:
            scene_number = _safe_int(item, 0)
            if scene_number > 0 and scene_number not in priority_scene_numbers:
                priority_scene_numbers.append(scene_number)

    findings: list[dict[str, Any]] = []
    raw_findings = value.get("distribution_findings")
    if isinstance(raw_findings, list):
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            family = str(item.get("family") or "").strip()
            severity = str(item.get("severity") or "").strip().lower()
            if severity not in ISSUE_SEVERITIES:
                severity = "medium"
            scene_numbers: list[int] = []
            raw_numbers = item.get("scene_numbers")
            if isinstance(raw_numbers, list):
                for raw_number in raw_numbers:
                    scene_number = _safe_int(raw_number, 0)
                    if scene_number > 0 and scene_number not in scene_numbers:
                        scene_numbers.append(scene_number)
            findings.append(
                {
                    "family": family or "other",
                    "severity": severity,
                    "scene_numbers": scene_numbers,
                    "description": str(item.get("description") or "").strip(),
                }
            )

    return {
        "priority_scene_numbers": priority_scene_numbers,
        "distribution_findings": findings,
    }


def _merge_act_dialogue_report(*, base_report: dict[str, Any], llm_report: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_report)
    existing_priority = [
        _safe_int(scene_number, 0)
        for scene_number in base_report.get("priority_scene_numbers", [])
        if _safe_int(scene_number, 0) > 0
    ]
    llm_priority = [
        _safe_int(scene_number, 0)
        for scene_number in llm_report.get("priority_scene_numbers", [])
        if _safe_int(scene_number, 0) > 0
    ]
    merged["llm_priority_scene_numbers"] = llm_priority
    merged["priority_scene_numbers"] = _dedupe_preserve_order(existing_priority + llm_priority)
    merged["distribution_findings"] = list(llm_report.get("distribution_findings") or [])
    return merged


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
