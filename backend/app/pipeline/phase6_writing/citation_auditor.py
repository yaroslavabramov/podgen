"""Phase 6.3: Citation audit + conditional repair."""

from __future__ import annotations

import json
import logging
import time
import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.models.claim import Claim
from app.models.script_segment import AuditStatus, ScriptSegment
from app.models.show_bible import ShowBible
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.phase6_writing.dialogue_utils import (
    dedupe_preserve_order as _dedupe_preserve_order,
    extract_dialogue_claim_ids as _extract_dialogue_claim_ids,
    load_manifest_scene_metadata_lookup as _load_manifest_scene_metadata_lookup,
    merge_dialogue_metadata as _merge_dialogue_metadata,
    normalize_dialogue_output as _normalize_dialogue_output,
    set_segment_audit_finding as _set_segment_audit_finding,
)
from app.pipeline.state import PipelineState
from app.pipeline.step_control import StepExecutionStopped
from app.realtime.events import phase_completed, progress, quality_update, step_completed, step_started
from app.realtime.publisher import publish_event
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

CITATION_SEVERITIES = {"low", "medium", "high"}
CITATION_SCOPES = {"record", "repair", "block"}
ISSUE_LINE_FUNCTIONS = {
    "assertive",
    "question_open",
    "question_with_premise",
    "recap",
    "transition",
    "synthesis",
    "other",
}
MAX_REPAIR_LINE_DRIFT_RATIO = 0.3
MAX_LINE_REPAIR_TARGETS = 2

AUDITOR_SYSTEM = """You are a citation auditor for a podcast script.
Check each dialogue line against the claims and policies.

Return JSON:
{
  "passed": true,
  "issues": [
    {
      "line_index": 0,
      "type": "unverified_stated_as_fact|forbidden_phrase|missing_attribution|audience_mismatch|missing_claim_link|temp_source_only_claim|disputed_without_balance|unsupported_source_framing",
      "severity": "low|medium|high",
      "scope": "record|repair|block",
      "line_function": "assertive|question_open|question_with_premise|recap|transition|synthesis|other",
      "action_required": true,
      "description": "what is wrong",
      "suggestion": "how to fix it"
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
1. Every factual statement must link to a claim_id.
2. UNVERIFIED claims cannot be stated confidently.
3. DISPUTED claims must present both sides or clear uncertainty.
4. OPINION/REPORT claims need attribution.
4a. Explicit source framing is only valid when `available_claims` actually support that attribution/source frame.
4b. If a line is mostly supported but one quoted, sourced, or over-specific fragment is not, flag the unsupported fragment without treating the whole surrounding explanation as unsupported by default.
5. No forbidden phrases.
6. Audience level must match the show.
7. Do not require `claim_ids` on purely conversational lines that do not add a new factual assertion.
8. Questions, callbacks, recaps, scene transitions, and synthesis lines should only be flagged for `missing_claim_link` if they introduce a new unsupported fact.
9. Set `line_function` for each issue so downstream normalization can distinguish assertive lines from open questions, recap questions, transitions, and synthesis.
10. Set `action_required=false` when the issue is informational only and should not trigger repair, even if the line is being mentioned for audit visibility.

Severity guide:
- `low`: acceptable for now, record it only
- `medium`: should be repaired before audio if practical
- `high`: must be repaired or block audio

Scope guide:
- `record`: informational only
- `repair`: attempt a repair pass
- `block`: if unresolved after repair, block audio

Language contract:
- The payload includes `language`.
- Audit the dialogue in that language and treat spoken questions, recaps, and transitions according to their function in that language.
- Do not assume English phrasing conventions when judging whether a line is assertive, recap-like, or purely conversational."""

LINE_REPAIR_SYSTEM = """You are repairing factual-rigor issues in specific podcast dialogue lines.

Return JSON:
{
  "repaired_lines": [
    {"line_index": 0, "speaker": "A", "text": "line", "claim_ids": ["id"]}
  ]
}

Rules:
- This is spoken podcast dialogue, not an article, memo, or legal brief.
- The payload includes `language`; write naturally in that language and in the same spoken register as the original.
- Preserve the original line's spoken rhythm, sentence shape, and scene function.
- Keep named actors and agency if they are not themselves the unsupported part.
- Rewrite only the targeted lines.
- Keep each targeted line on the same `line_index` and preserve its speaker.
- If the original line is a question, bridge, or recap, keep it non-assertive unless the issue explicitly requires a factual clarification.
- Fix only the flagged citation / rigor issues.
- Strategy order:
  1. reuse an existing supporting `claim_id` if `available_claims` already support the line
  2. keep the content but narrow or hedge only the unsupported fragment
  3. replace over-precise wording with a nearby supported version
  4. delete content only as a last resort
- Use explicit source framing only when the supporting claims actually include attribution/source support. Do not invent a source frame that is absent from `available_claims`.
- If `available_claims` mark a claim as `verified`, you may remove unnecessary caution.
- If a claim is `observed` or `unverified`, do not increase confidence beyond that support level.
- Do not default to bureaucratic, legalistic, or academic hedging. Prefer short spoken uncertainty markers that sound natural in the target language.
- Keep the line close in length to the original. If the original is one sentence, prefer one sentence.
- Use at most one short uncertainty marker in a line.
- Do not turn an internal bridge into sign-off or next-episode teaser language unless `scene_context.is_final_scene_in_episode` is true.
- Do not add new facts, institutions, dates, figures, or claims.
- Do not rewrite or comment on unflagged lines.
- The repaired line must sound natural when read aloud.

Pattern guidance:
- If exact timing is unsupported, keep the event but lower the precision.
- If a mechanism explanation is mostly supported, keep the mechanism and narrow only the unsupported step.
- If a bridge line contains one unsupported factual fragment, repair that fragment without turning the line into a teaser or sign-off.
- If a vivid action/agency chain is mostly supported, keep the actors and narrow only the unsupported link.
- If a line sounds quote-like or source-like but `available_claims` do not support that framing, drop the unsupported framing and keep the supported substance.
- If the line already carries uncertainty, do not stack multiple hedges on top of each other.
- Output only JSON."""

REPAIR_SYSTEM = """You are repairing factual-rigor issues in a podcast dialogue scene.

Return JSON:
{
  "dialogue": [
    {"speaker": "A", "text": "line", "claim_ids": ["id"]},
    {"speaker": "B", "text": "line", "claim_ids": []}
  ]
}

Rules:
- Fix only the citation and rigor issues that were flagged.
- This is spoken podcast dialogue, not an article, memo, or legal brief.
- The payload includes `language`; write naturally in that language and in the same spoken register as the original.
- Preserve the scene's factual meaning whenever supported by available claims.
- Focus edits on lines flagged as `block` or `repair`; keep clean lines as unchanged as possible.
- Preserve the scene's dramatic structure, pacing, and agency, not just its factual gist.
- If one clause is unsupported, repair that clause and leave the surrounding story intact.
- Keep named actors and actions if they are not themselves the unsupported part.
- Strategy order:
  1. reuse an existing supporting `claim_id` if `available_claims` already support the line
  2. keep the content but narrow or hedge only the unsupported fragment
  3. replace over-precise wording with a nearby supported version
  4. delete content only as a last resort
- Use explicit source framing only when the supporting claims actually include attribution/source support. Do not invent a source frame that is absent from `available_claims`.
- If `available_claims` mark a claim as `verified`, you may remove unnecessary caution.
- If a claim is `observed` or `unverified`, do not increase confidence beyond that support level.
- Add hedging or attribution where required, but do not default to bureaucratic, legalistic, or academic phrasing. Prefer the shortest spoken form that fits the target language.
- Do not flatten a vivid story beat into generalized exposition just to remove one unsupported assertion.
- Do not add new facts, institutions, dates, figures, or claims.
- Preserve the scene's `scene_job`, `scene_type`, and content function.
- Prefer the smallest edit that fixes rigor. Do not re-author the whole scene unless necessary.
- Do not turn non-assertive lines into new exposition.
- Do not turn a mid-episode bridge into sign-off or next-episode teaser language unless `scene_context.is_final_scene_in_episode` is true.
- Keep the line count close to the original scene length.
- Use at most one short uncertainty marker per line.
- Keep only speakers `A` and `B`.
- Preserve natural spoken register in the target language instead of rewriting into abstract summary language.
- If exact timing is unsupported, lower precision without deleting the scene's narrative move.
- If one causal or agency link is unsupported, narrow that link while keeping the surrounding story structure.
- If a mechanism explanation is mostly supported, keep the mechanism and narrow only the unsupported step instead of flattening the whole explanation.
- If a line sounds quote-like or explicitly source-framed but `available_claims` do not support that framing, remove the unsupported framing and keep the supported substance.
- If a line already contains uncertainty, do not stack multiple hedges on top of it.
- The repaired scene must sound natural when read aloud.
- Output only JSON."""


async def audit_citations(state: PipelineState) -> dict:
    """LangGraph node: audit all script segments for citation accuracy and repair what is fixable."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, step_started("phase6_writing", "citation_audit", "Auditing citations and repairing rigor issues"))

    segment_ids = _dedupe_preserve_order(state.get("script_segment_ids", []))
    quality_signals = dict(state.get("quality_signals", {}))

    async with async_session_factory() as db:
        bible = None
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()

        forbidden = bible.forbidden_phrases if bible else []
        rigor = bible.rigor_policy if bible else ""

        claims_map = await _load_claims_map(db, state.get("claim_ids", []))
        scene_metadata_lookup = await _load_manifest_scene_metadata_lookup(db, state.get("manifest_id"))

        total_metrics = {
            "fact_coverage_ratio": 0.0,
            "forbidden_phrase_hits": 0.0,
            "rigor_policy_violations": 0.0,
            "persona_consistency_score": 0.0,
            "temp_source_only_claims": 0.0,
        }
        segment_count = 0
        repaired_scenes = 0
        failed_scenes = 0
        blocking_scenes: list[str] = []

        for sid in segment_ids:
            try:
                segment_uuid = uuid.UUID(sid)
            except (TypeError, ValueError):
                continue
            result = await db.execute(select(ScriptSegment).where(ScriptSegment.id == segment_uuid))
            segment = result.scalar_one_or_none()
            if segment is None:
                continue
            scene_metadata = scene_metadata_lookup.get((segment.act_number, segment.scene_number), {})
            scene_claims_map = _select_scene_claims_map(
                claims_map=claims_map,
                dialogue=segment.dialogue_json,
                scene_claim_ids=scene_metadata.get("claim_ids", []),
            )

            prompt_payload = _build_citation_audit_prompt_payload(
                dialogue=segment.dialogue_json,
                claims_map=scene_claims_map,
                forbidden_phrases=forbidden,
                rigor_policy=rigor,
                language=state["language"],
                scene_context=_build_scene_context(
                    act_number=segment.act_number,
                    scene_number=segment.scene_number,
                    scene_metadata=scene_metadata,
                ),
            )

            try:
                audit = await _run_citation_audit_with_retry(prompt_payload)
            except StepExecutionStopped:
                raise
            except Exception as exc:
                logger.warning("Citation audit failed for segment %s", sid, exc_info=True)
                failure_audit = _build_failed_citation_audit(
                    error=f"{type(exc).__name__}: {exc}",
                )
                _dump_citation_audit_debug(
                    run_id=run_id,
                    act_number=segment.act_number,
                    scene_number=segment.scene_number,
                    status="llm_or_parse_failed",
                    prompt_payload=prompt_payload,
                    audit=failure_audit,
                    error=f"{type(exc).__name__}: {exc}",
                )
                segment.quality_metrics = failure_audit["metrics"]
                segment.audit_status = AuditStatus.FAILED
                _set_segment_audit_finding(
                    segment,
                    "citation",
                    {
                        "passed": False,
                        "issues": failure_audit["issues"],
                        "metrics": failure_audit["metrics"],
                        "repair_attempted": False,
                        "repair_accepted": False,
                        "blocking": True,
                    },
                )
                failed_scenes += 1
                blocking_scenes.append(f"A{segment.act_number}S{segment.scene_number}")
                for key in total_metrics:
                    total_metrics[key] += float(failure_audit["metrics"].get(key, 0) or 0)
                segment_count += 1
                break

            _dump_citation_audit_debug(
                run_id=run_id,
                act_number=segment.act_number,
                scene_number=segment.scene_number,
                status="ok",
                prompt_payload=prompt_payload,
                audit=audit,
                error=None,
            )

            final_dialogue = segment.dialogue_json
            final_audit = audit
            repair_attempted = False
            repair_accepted = False

            if audit["issues"] and _should_attempt_citation_repair(audit["issues"]):
                repair_attempted = True
                repaired_dialogue, repaired_audit, repair_accepted = await _attempt_citation_repair(
                    run_id=run_id,
                    segment=segment,
                    claims_map=scene_claims_map,
                    forbidden_phrases=forbidden,
                    rigor_policy=rigor,
                    language=state["language"],
                    original_audit=audit,
                    scene_context=_build_scene_context(
                        act_number=segment.act_number,
                        scene_number=segment.scene_number,
                        scene_metadata=scene_metadata,
                    ),
                )
                if repair_accepted and repaired_dialogue is not None and repaired_audit is not None:
                    final_dialogue = repaired_dialogue
                    final_audit = repaired_audit
                    repaired_scenes += 1

            segment.dialogue_json = final_dialogue
            segment.quality_metrics = final_audit["metrics"]
            segment.audit_status = AuditStatus.PASSED if final_audit["passed"] else AuditStatus.FAILED
            _set_segment_audit_finding(
                segment,
                "citation",
                {
                    "passed": final_audit["passed"],
                    "issues": final_audit["issues"],
                    "metrics": final_audit["metrics"],
                    "repair_attempted": repair_attempted,
                    "repair_accepted": repair_accepted,
                    "blocking": _has_blocking_citation_issues(final_audit["issues"]),
                },
            )

            if not final_audit["passed"]:
                failed_scenes += 1
            if _has_blocking_citation_issues(final_audit["issues"]):
                blocking_scenes.append(f"A{segment.act_number}S{segment.scene_number}")

            for key in total_metrics:
                total_metrics[key] += float(final_audit["metrics"].get(key, 0) or 0)
            segment_count += 1
            if _has_blocking_citation_issues(final_audit["issues"]):
                break

        await db.commit()

    if segment_count > 0:
        for key in total_metrics:
            total_metrics[key] = round(total_metrics[key] / segment_count, 2)

    quality_signals.update(total_metrics)
    quality_signals["citation_failed_scenes"] = failed_scenes
    quality_signals["citation_repaired_scenes"] = repaired_scenes
    quality_signals["citation_blocking_scenes"] = blocking_scenes
    quality_signals["audio_blocked"] = bool(blocking_scenes)
    publish_event(
        run_id,
        quality_update(
            {
                **total_metrics,
                "citation_blocking_scenes": blocking_scenes,
                "audio_blocked": bool(blocking_scenes),
            }
        ),
    )

    if blocking_scenes:
        raise RuntimeError(
            "Citation audit blocked audio because unresolved high-severity issues remain in "
            + ", ".join(blocking_scenes)
        )

    publish_event(run_id, step_completed("phase6_writing", "citation_audit"))
    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase6_writing", duration_ms))
    publish_event(run_id, progress(80.0, "Citation audit complete"))

    return {"script_segment_ids": segment_ids, "quality_signals": quality_signals, "current_phase": "phase6_writing"}


async def _load_claims_map(db, claim_ids: list[str]) -> dict[str, dict[str, Any]]:
    claims_map: dict[str, dict[str, Any]] = {}
    claim_uuids: list[uuid.UUID] = []
    for cid in claim_ids:
        try:
            claim_uuids.append(uuid.UUID(cid))
        except (TypeError, ValueError):
            continue
    if not claim_uuids:
        return claims_map

    result = await db.execute(select(Claim).where(Claim.id.in_(claim_uuids)))
    for claim in result.scalars():
        has_attribution = bool(claim.attribution)
        claims_map[str(claim.id)] = {
            "type": claim.claim_type,
            "verification": claim.verification_level,
            "statement": claim.statement,
            "attribution": claim.attribution,
            "has_attribution": has_attribution,
            "supports_source_framing": has_attribution or claim.claim_type in {"opinion", "report"},
        }
    return claims_map


def _build_citation_audit_prompt_payload(
    *,
    dialogue: list[dict[str, Any]],
    claims_map: dict[str, dict[str, Any]],
    forbidden_phrases: list[str],
    rigor_policy: str,
    language: str,
    scene_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "dialogue": dialogue,
        "scene_context": scene_context,
        "available_claims": claims_map,
        "forbidden_phrases": forbidden_phrases,
        "rigor_policy": rigor_policy,
        "language": language,
    }


def _build_failed_citation_audit(*, error: str) -> dict[str, Any]:
    return {
        "passed": False,
        "issues": [
            {
                "line_index": -1,
                "type": "audit_failure",
                "severity": "high",
                "scope": "block",
                "description": "Citation audit could not complete, so the scene remains unaudited.",
                "suggestion": error,
            }
        ],
        "metrics": _normalize_metrics({}),
    }


async def _run_citation_audit(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    raw = await call_haiku_structured(
        system=AUDITOR_SYSTEM,
        prompt=json.dumps(prompt_payload, ensure_ascii=False),
        max_tokens=2048,
        temperature=0.2,
    )
    audit = parse_llm_json(raw)
    issues = _post_filter_citation_issues(
        dialogue=prompt_payload.get("dialogue"),
        issues=_normalize_citation_issues(audit.get("issues")),
    )
    metrics = _normalize_metrics(audit.get("metrics"))
    passed = not any(_issue_requires_action(issue) for issue in issues)
    return {"passed": passed, "issues": issues, "metrics": metrics}


async def _run_citation_audit_with_retry(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    max_attempts = max(1, int(settings.max_citation_audit_attempts))
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await _run_citation_audit(prompt_payload)
        except StepExecutionStopped:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            logger.warning(
                "Citation audit attempt %d/%d failed; retrying",
                attempt,
                max_attempts,
                exc_info=True,
            )
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Citation audit retry loop exited without returning or raising")


async def _attempt_citation_repair(
    *,
    run_id: str,
    segment: ScriptSegment,
    claims_map: dict[str, dict[str, Any]],
    forbidden_phrases: list[str],
    rigor_policy: str,
    language: str,
    original_audit: dict[str, Any],
    scene_context: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, bool]:
    original_dialogue = segment.dialogue_json
    original_line_count = len(original_dialogue) if isinstance(original_dialogue, list) else 0
    max_attempts = 1 if not _has_blocking_citation_issues(original_audit["issues"]) else max(
        1, int(settings.max_citation_repair_attempts)
    )
    repair_attempts = _build_citation_repair_attempt_plan(
        original_dialogue,
        scene_context,
        original_audit["issues"],
        max_attempts=max_attempts,
    )
    retry_feedback = ""
    for attempt, repair_attempt in enumerate(repair_attempts, start=1):
        repair_mode = repair_attempt["mode"]
        target_line_indexes = repair_attempt["target_line_indexes"]
        prompt_payload = _build_citation_repair_prompt_payload(
            repair_mode=repair_mode,
            scene_context=scene_context,
            original_line_count=original_line_count,
            dialogue=original_dialogue,
            claims_map=claims_map,
            forbidden_phrases=forbidden_phrases,
            rigor_policy=rigor_policy,
            language=language,
            issues=original_audit["issues"],
            target_line_indexes=target_line_indexes,
        )
        if retry_feedback:
            prompt_payload["retry_feedback"] = retry_feedback

        try:
            repaired_dialogue = await _run_citation_repair_attempt(
                repair_mode=repair_mode,
                prompt_payload=prompt_payload,
                original_dialogue=original_dialogue,
                target_line_indexes=target_line_indexes,
            )
        except StepExecutionStopped:
            raise
        except Exception as exc:
            _dump_citation_repair_debug(
                run_id=run_id,
                act_number=segment.act_number,
                scene_number=segment.scene_number,
                status="llm_or_parse_failed",
                payload=prompt_payload,
                before_dialogue=original_dialogue,
                after_dialogue=None,
                before_audit=original_audit,
                after_audit=None,
                decision="rejected",
                error=f"{type(exc).__name__}: {exc}",
                attempt=attempt,
                max_attempts=len(repair_attempts),
            )
            if attempt >= len(repair_attempts):
                return None, None, False
            retry_feedback = f"Previous repair attempt failed with {type(exc).__name__}: {exc}."
            continue

        if not repaired_dialogue:
            _dump_citation_repair_debug(
                run_id=run_id,
                act_number=segment.act_number,
                scene_number=segment.scene_number,
                status="empty_dialogue",
                payload=prompt_payload,
                before_dialogue=original_dialogue,
                after_dialogue=repaired_dialogue,
                before_audit=original_audit,
                after_audit=None,
                decision="rejected",
                error="repair produced no usable dialogue",
                attempt=attempt,
                max_attempts=len(repair_attempts),
            )
            if attempt >= len(repair_attempts):
                return None, None, False
            retry_feedback = "Previous repair attempt produced no usable dialogue."
            continue

        repaired_audit = await _run_citation_audit_with_retry(
            _build_citation_audit_prompt_payload(
                dialogue=repaired_dialogue,
                claims_map=claims_map,
                forbidden_phrases=forbidden_phrases,
                rigor_policy=rigor_policy,
                language=language,
                scene_context=scene_context,
            )
        )
        accepted = _citation_repair_improved(
            original_audit,
            repaired_audit,
            original_dialogue=original_dialogue,
            repaired_dialogue=repaired_dialogue,
            repair_mode=repair_mode,
            target_line_indexes=target_line_indexes,
            original_line_count=original_line_count,
            repaired_line_count=len(repaired_dialogue),
        )
        _dump_citation_repair_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="accepted" if accepted else "rejected",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=repaired_dialogue,
            before_audit=original_audit,
            after_audit=repaired_audit,
            decision="accepted" if accepted else "rejected",
            error=None,
            attempt=attempt,
            max_attempts=len(repair_attempts),
        )
        if accepted:
            return repaired_dialogue, repaired_audit, True
        if attempt >= len(repair_attempts):
            return None, None, False
        retry_feedback = _build_citation_repair_retry_feedback(
            original_audit=original_audit,
            repaired_audit=repaired_audit,
        )

    return None, None, False


def _build_citation_repair_attempt_plan(
    dialogue: list[dict[str, Any]],
    scene_context: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    max_attempts: int,
) -> list[dict[str, Any]]:
    primary_mode, target_line_indexes = _select_citation_repair_mode(dialogue=dialogue, issues=issues)
    attempts: list[dict[str, Any]] = [{"mode": primary_mode, "target_line_indexes": target_line_indexes}]
    if max_attempts <= 1:
        return attempts

    sensitive_indexes = _sensitive_line_indexes(dialogue)
    actionable_indexes = set(_actionable_line_indexes(issues))
    fallback_mode = (
        "line_repair"
        if actionable_indexes
        and actionable_indexes.issubset(sensitive_indexes)
        and len(actionable_indexes) <= MAX_LINE_REPAIR_TARGETS
        else "scene_repair"
    )
    while len(attempts) < max_attempts:
        fallback_targets = (
            sorted(actionable_indexes)[:MAX_LINE_REPAIR_TARGETS]
            if fallback_mode == "line_repair"
            else []
        )
        attempts.append({"mode": fallback_mode, "target_line_indexes": fallback_targets})
    return attempts


def _select_citation_repair_mode(*, dialogue: list[dict[str, Any]], issues: list[dict[str, Any]]) -> tuple[str, list[int]]:
    actionable = _actionable_citation_issues(issues)
    if not actionable:
        return "scene_repair", []

    line_indexes = sorted(
        {
            _issue_line_index(issue)
            for issue in actionable
            if _issue_line_index(issue) >= 0
        }
    )
    if (
        line_indexes
        and len(line_indexes) <= MAX_LINE_REPAIR_TARGETS
        and all(_issue_line_index(issue) in line_indexes for issue in actionable)
    ):
        return "line_repair", line_indexes
    sensitive_indexes = _sensitive_line_indexes(dialogue)
    if line_indexes and set(line_indexes).issubset(sensitive_indexes):
        if len(line_indexes) <= MAX_LINE_REPAIR_TARGETS:
            return "line_repair", line_indexes
        return "scene_repair", []
    return "scene_repair", []


def _build_citation_repair_prompt_payload(
    *,
    repair_mode: str,
    scene_context: dict[str, Any],
    original_line_count: int,
    dialogue: list[dict[str, Any]],
    claims_map: dict[str, dict[str, Any]],
    forbidden_phrases: list[str],
    rigor_policy: str,
    language: str,
    issues: list[dict[str, Any]],
    target_line_indexes: list[int],
) -> dict[str, Any]:
    flagged_line_indexes = _actionable_line_indexes(issues)
    payload = {
        "repair_mode": repair_mode,
        "scene_context": scene_context,
        "original_line_count": original_line_count,
        "dialogue": dialogue,
        "available_claims": claims_map,
        "forbidden_phrases": forbidden_phrases,
        "rigor_policy": rigor_policy,
        "language": language,
        "flagged_line_indexes": flagged_line_indexes,
        "line_roles": _build_line_roles(dialogue),
    }
    if repair_mode == "line_repair":
        payload["target_line_indexes"] = target_line_indexes
        payload["target_lines"] = [
            {
                "line_index": line_index,
                "dialogue": _dialogue_line(dialogue, line_index),
                "issues": _issues_for_line_indexes(issues, [line_index]),
            }
            for line_index in target_line_indexes
        ]
    else:
        payload["issues"] = issues
        payload["flagged_lines"] = [
            {
                "line_index": line_index,
                "dialogue": _dialogue_line(dialogue, line_index),
                "issues": _issues_for_line_indexes(issues, [line_index]),
            }
            for line_index in flagged_line_indexes
        ]
    return payload


async def _run_citation_repair_attempt(
    *,
    repair_mode: str,
    prompt_payload: dict[str, Any],
    original_dialogue: list[dict[str, Any]],
    target_line_indexes: list[int],
) -> list[dict[str, Any]] | None:
    raw = await call_sonnet_structured(
        system=LINE_REPAIR_SYSTEM if repair_mode == "line_repair" else REPAIR_SYSTEM,
        prompt=json.dumps(prompt_payload, ensure_ascii=False),
        max_tokens=2048,
        temperature=0.2,
    )
    parsed = parse_llm_json(raw)
    if repair_mode == "line_repair":
        repaired_lines = _normalize_line_repair_output(parsed.get("repaired_lines"))
        return _splice_repaired_lines(
            original_dialogue=original_dialogue,
            repaired_lines=repaired_lines,
            target_line_indexes=target_line_indexes,
        )
    repaired_dialogue = _normalize_dialogue_output(parsed.get("dialogue"))
    return _merge_scene_repair_metadata(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
    )


def _normalize_line_repair_output(value: Any) -> dict[int, dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    repaired_lines: dict[int, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            line_index = int(item.get("line_index"))
        except (TypeError, ValueError):
            continue
        normalized_line = _normalize_dialogue_output(
            [
                {
                    "speaker": item.get("speaker"),
                    "text": item.get("text"),
                    "claim_ids": item.get("claim_ids"),
                }
            ]
        )
        if not normalized_line:
            continue
        repaired_lines[line_index] = normalized_line[0]
    return repaired_lines or None


def _splice_repaired_lines(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_lines: dict[int, dict[str, Any]] | None,
    target_line_indexes: list[int],
) -> list[dict[str, Any]] | None:
    if not repaired_lines:
        return None
    if any(line_index not in repaired_lines for line_index in target_line_indexes):
        return None

    spliced: list[dict[str, Any]] = []
    for line in original_dialogue:
        if not isinstance(line, dict):
            return None
        spliced.append(deepcopy(line))
    for line_index in target_line_indexes:
        if line_index < 0 or line_index >= len(spliced):
            return None
        repaired_line = repaired_lines[line_index]
        merged_line = deepcopy(spliced[line_index])
        merged_line["speaker"] = repaired_line.get("speaker")
        merged_line["text"] = repaired_line.get("text")
        merged_line["claim_ids"] = list(repaired_line.get("claim_ids") or [])
        spliced[line_index] = merged_line
    return spliced


def _merge_scene_repair_metadata(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    return _merge_dialogue_metadata(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
    )


def _normalize_citation_issues(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        issue_type = item.get("type")
        if not isinstance(issue_type, str) or not issue_type.strip():
            continue
        line_index = item.get("line_index")
        try:
            normalized_line_index = int(line_index)
        except (TypeError, ValueError):
            normalized_line_index = -1

        severity = str(item.get("severity") or "medium").strip().lower()
        if severity not in CITATION_SEVERITIES:
            severity = "medium"
        scope = str(item.get("scope") or "").strip().lower()
        if scope not in CITATION_SCOPES:
            if severity == "high":
                scope = "block"
            elif severity == "medium":
                scope = "repair"
            else:
                scope = "record"
        line_function = str(item.get("line_function") or "").strip().lower()
        if line_function not in ISSUE_LINE_FUNCTIONS:
            line_function = "other"
        raw_action_required = item.get("action_required")
        if isinstance(raw_action_required, bool):
            action_required = raw_action_required
        else:
            action_required = scope in {"repair", "block"}

        normalized.append(
            {
                "line_index": normalized_line_index,
                "type": issue_type.strip(),
                "severity": severity,
                "scope": scope,
                "line_function": line_function,
                "action_required": action_required,
                "description": str(item.get("description") or "").strip(),
                "suggestion": str(item.get("suggestion") or "").strip(),
            }
        )
    return normalized


def _post_filter_citation_issues(*, dialogue: object, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for issue in issues:
        line = _dialogue_line(dialogue, _issue_line_index(issue))
        if line is None:
            filtered.append(issue)
            continue

        text = str(line.get("text") or "").strip()
        if not text:
            filtered.append(issue)
            continue

        if _line_is_question(text) or _issue_has_non_assertive_signal(issue):
            filtered.append(_downgrade_non_assertive_issue(issue, text=text, question=_line_is_question(text)))
            continue
        filtered.append(issue)
    return filtered


def _downgrade_non_assertive_issue(issue: dict[str, Any], *, text: str, question: bool) -> dict[str, Any]:
    issue_type = str(issue.get("type") or "").strip()
    if issue_type not in {"missing_claim_link", "unverified_stated_as_fact"}:
        return issue

    downgraded = dict(issue)
    has_fact_markers = _line_has_concrete_fact_markers(text)
    line_function = str(downgraded.get("line_function") or "").strip().lower()
    action_required = downgraded.get("action_required")
    non_assertive = line_function in {"question_open", "transition", "synthesis"}
    informational_non_assertive = action_required is False or (
        question and line_function == "question_open" and action_required is not True
    )
    if issue_type == "missing_claim_link":
        if (question or non_assertive) and not has_fact_markers and informational_non_assertive:
            downgraded["severity"] = "low"
            downgraded["scope"] = "record"
            downgraded["action_required"] = False
            return downgraded
        if downgraded.get("scope") == "record" or downgraded.get("severity") == "low":
            return downgraded
        downgraded["severity"] = "medium"
        downgraded["scope"] = "repair"
        downgraded["action_required"] = True
        return downgraded

    if (question or non_assertive) and not has_fact_markers and informational_non_assertive:
        downgraded["severity"] = "low"
        downgraded["scope"] = "record"
        downgraded["action_required"] = False
        return downgraded

    if downgraded.get("scope") == "block":
        downgraded["scope"] = "repair"
    if downgraded.get("severity") == "high":
        downgraded["severity"] = "medium"
    downgraded["action_required"] = True
    return downgraded


def _dialogue_line(dialogue: object, line_index: int) -> dict[str, Any] | None:
    if not isinstance(dialogue, list):
        return None
    if line_index < 0 or line_index >= len(dialogue):
        return None
    line = dialogue[line_index]
    return line if isinstance(line, dict) else None


def _line_is_question(text: str) -> bool:
    return text.rstrip().endswith("?")


def _issue_has_non_assertive_signal(issue: dict[str, Any]) -> bool:
    line_function = str(issue.get("line_function") or "").strip().lower()
    return line_function in {"question_open", "transition", "synthesis"}


def _line_has_concrete_fact_markers(text: str) -> bool:
    return any(char.isdigit() for char in text)


def _normalize_metrics(value: Any) -> dict[str, float]:
    metrics = {
        "fact_coverage_ratio": 0.0,
        "forbidden_phrase_hits": 0.0,
        "rigor_policy_violations": 0.0,
        "persona_consistency_score": 0.0,
        "temp_source_only_claims": 0.0,
    }
    if not isinstance(value, dict):
        return metrics
    for key in metrics:
        raw = value.get(key, metrics[key])
        try:
            metrics[key] = float(raw)
        except (TypeError, ValueError):
            metrics[key] = metrics[key]
    return metrics


def _citation_severity_rank(value: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(value, 0)


def _worst_citation_severity(issues: list[dict[str, Any]]) -> int:
    return max((_citation_severity_rank(str(issue.get("severity"))) for issue in issues), default=0)


def _build_scene_context(
    *,
    act_number: int,
    scene_number: int,
    scene_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    scene_metadata = scene_metadata if isinstance(scene_metadata, dict) else {}
    return {
        "act_number": act_number,
        "scene_number": scene_number,
        "scene_job": str(scene_metadata.get("scene_job") or "").strip(),
        "scene_type": str(scene_metadata.get("scene_type") or "").strip(),
        "description": str(scene_metadata.get("description") or "").strip(),
        "is_final_scene_in_act": bool(scene_metadata.get("is_final_scene_in_act")),
        "is_final_scene_in_episode": bool(scene_metadata.get("is_final_scene_in_episode")),
    }


def _select_scene_claims_map(
    *,
    claims_map: dict[str, dict[str, Any]],
    dialogue: object,
    scene_claim_ids: list[str],
) -> dict[str, dict[str, Any]]:
    ordered_claim_ids = _dedupe_preserve_order(
        _extract_dialogue_claim_ids(dialogue) + [cid for cid in scene_claim_ids if isinstance(cid, str)]
    )
    subset: dict[str, dict[str, Any]] = {}
    for claim_id in ordered_claim_ids:
        claim_payload = claims_map.get(claim_id)
        if claim_payload is None:
            continue
        subset[claim_id] = claim_payload
    return subset


def _blocking_issue_signature(issue: dict[str, Any]) -> tuple[int, str]:
    return (
        _issue_line_index(issue),
        str(issue.get("type") or "").strip(),
    )


def _blocking_issue_signatures(issues: list[dict[str, Any]]) -> set[tuple[int, str]]:
    return {
        _blocking_issue_signature(issue)
        for issue in issues
        if _issue_is_blocking(issue)
    }


def _line_count_drift_exceeds(*, original_line_count: int, repaired_line_count: int) -> bool:
    baseline = max(original_line_count, 1)
    return abs(repaired_line_count - original_line_count) / baseline > MAX_REPAIR_LINE_DRIFT_RATIO


def _should_attempt_citation_repair(issues: list[dict[str, Any]]) -> bool:
    return any(
        _issue_requires_action(issue) and _citation_severity_rank(str(issue.get("severity"))) >= 2
        for issue in issues
    )


def _has_blocking_citation_issues(issues: list[dict[str, Any]]) -> bool:
    return any(_issue_is_blocking(issue) for issue in issues)


def _actionable_citation_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in issues if _issue_requires_action(issue)]


def _issues_for_line_indexes(issues: list[dict[str, Any]], line_indexes: list[int]) -> list[dict[str, Any]]:
    allowed = set(line_indexes)
    return [
        issue
        for issue in issues
        if isinstance(issue, dict) and _issue_line_index(issue) in allowed
    ]


def _actionable_line_indexes(issues: list[dict[str, Any]]) -> list[int]:
    return sorted(
        {
            _issue_line_index(issue)
            for issue in issues
            if _issue_requires_action(issue) and _issue_line_index(issue) >= 0
        }
    )


def _line_count(dialogue: list[dict[str, Any]]) -> int:
    return len(dialogue) if isinstance(dialogue, list) else 0


def _line_role_for_index(dialogue: list[dict[str, Any]], line_index: int) -> str:
    line_count = _line_count(dialogue)
    if line_count <= 0 or line_index < 0 or line_index >= line_count:
        return "other"
    if line_index == 0:
        return "scene_opener"
    if line_index == line_count - 1:
        return "landing"
    if line_index == line_count - 2:
        return "punchline"
    if line_index == 1:
        return "setup_question"
    return "other"


def _build_line_roles(dialogue: list[dict[str, Any]]) -> dict[int, str]:
    return {
        line_index: _line_role_for_index(dialogue, line_index)
        for line_index in range(_line_count(dialogue))
    }


def _sensitive_line_indexes(dialogue: list[dict[str, Any]]) -> set[int]:
    line_count = _line_count(dialogue)
    if line_count <= 0:
        return set()
    indexes = {0, line_count - 1}
    if line_count >= 2:
        indexes.add(line_count - 2)
    return {idx for idx in indexes if 0 <= idx < line_count}


def _summarize_citation_issues(issues: list[dict[str, Any]]) -> list[str]:
    summaries: list[str] = []
    for issue in issues:
        if not _issue_requires_action(issue):
            continue
        issue_type = str(issue.get("type") or "").strip() or "issue"
        line_index = _issue_line_index(issue)
        description = str(issue.get("description") or "").strip()
        line_label = f"line {line_index}" if line_index >= 0 else "scene-level"
        summary = f"{line_label}: {issue_type}"
        if description:
            summary += f" ({description})"
        summaries.append(summary)
        if len(summaries) >= 4:
            break
    return summaries


def _build_citation_repair_retry_feedback(*, original_audit: dict[str, Any], repaired_audit: dict[str, Any]) -> str:
    remaining_blockers = _summarize_citation_issues(
        [issue for issue in repaired_audit.get("issues", []) if _issue_is_blocking(issue)]
    )
    if remaining_blockers:
        return (
            "Previous repair attempt did not clear blocking issues. Focus on resolving these remaining blockers: "
            + "; ".join(remaining_blockers)
        )

    new_actionable = _summarize_citation_issues(repaired_audit.get("issues", []))
    original_actionable = _summarize_citation_issues(original_audit.get("issues", []))
    if new_actionable:
        return (
            "Previous repair attempt was rejected. Keep the scene function intact and resolve these actionable issues without introducing new ones: "
            + "; ".join(new_actionable)
        )
    if original_actionable:
        return (
            "Previous repair attempt did not improve the audit enough. Preserve the scene and address the original actionable issues more minimally: "
            + "; ".join(original_actionable)
        )
    return "Previous repair attempt was rejected. Preserve the original scene more closely and make only minimal rigor fixes."


def _issue_requires_action(issue: Any) -> bool:
    if not isinstance(issue, dict):
        return False
    scope = str(issue.get("scope") or "").strip().lower()
    if scope not in {"repair", "block"}:
        return False
    action_required = issue.get("action_required")
    if isinstance(action_required, bool):
        return action_required
    return True


def _issue_is_blocking(issue: Any) -> bool:
    if not isinstance(issue, dict):
        return False
    if str(issue.get("scope") or "").strip().lower() != "block":
        return False
    action_required = issue.get("action_required")
    if isinstance(action_required, bool):
        return action_required
    return True


def _issue_line_index(issue: dict[str, Any]) -> int:
    try:
        return int(issue.get("line_index", -1))
    except (TypeError, ValueError):
        return -1


def _citation_repair_improved(
    original: dict[str, Any],
    repaired: dict[str, Any],
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]],
    repair_mode: str,
    target_line_indexes: list[int],
    original_line_count: int,
    repaired_line_count: int,
) -> bool:
    if not _citation_claim_preservation_ok(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        repair_mode=repair_mode,
        target_line_indexes=target_line_indexes,
    ):
        return False
    if repair_mode == "line_repair" and not _line_repair_speakers_preserved(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        target_line_indexes=target_line_indexes,
    ):
        return False
    if _line_count_drift_exceeds(
        original_line_count=original_line_count,
        repaired_line_count=repaired_line_count,
    ):
        return False

    if repair_mode == "scene_repair" and not _scene_repair_preserves_unflagged_sensitive_lines(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        actionable_line_indexes=set(_actionable_line_indexes(original["issues"])),
    ):
        return False
    if repair_mode == "scene_repair" and not _changed_line_indexes(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
    ):
        return False

    original_actionable_issues = _actionable_citation_issues(original["issues"])
    original_fact_coverage = float(original["metrics"].get("fact_coverage_ratio", 0.0) or 0.0)
    repaired_fact_coverage = float(repaired["metrics"].get("fact_coverage_ratio", 0.0) or 0.0)
    if repaired_fact_coverage + 0.05 < original_fact_coverage:
        return False

    original_blockers = _blocking_issue_signatures(original["issues"])
    repaired_blockers = _blocking_issue_signatures(repaired["issues"])
    if repaired_blockers - original_blockers:
        return False

    if repair_mode == "line_repair":
        targeted_original_issues = _actionable_citation_issues(_issues_for_line_indexes(original["issues"], target_line_indexes))
        targeted_repaired_issues = _actionable_citation_issues(_issues_for_line_indexes(repaired["issues"], target_line_indexes))
        targeted_original_blockers = _blocking_issue_signatures(targeted_original_issues)
        targeted_repaired_blockers = _blocking_issue_signatures(targeted_repaired_issues)
        if targeted_original_blockers:
            return not targeted_repaired_blockers

        if repaired["passed"] and not original["passed"]:
            return True

        if len(targeted_repaired_issues) < len(targeted_original_issues):
            return True

        original_rank = _worst_citation_severity(targeted_original_issues)
        repaired_rank = _worst_citation_severity(targeted_repaired_issues)
        if repaired_rank < original_rank:
            return True
        return False

    if original_blockers:
        return not repaired_blockers

    relevant_line_indexes = _relevant_reaudit_line_indexes(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
        original_issues=original_actionable_issues,
        repair_mode=repair_mode,
        target_line_indexes=target_line_indexes,
    )
    original_relevant_issues = _actionable_citation_issues(
        _issues_for_line_indexes(original["issues"], relevant_line_indexes)
    )
    repaired_relevant_issues = _actionable_citation_issues(
        _issues_for_line_indexes(repaired["issues"], relevant_line_indexes)
    )

    if repaired["passed"] and not original["passed"]:
        return True

    if len(repaired_relevant_issues) < len(original_relevant_issues):
        return True

    original_rank = _worst_citation_severity(original_relevant_issues)
    repaired_rank = _worst_citation_severity(repaired_relevant_issues)
    if repaired_rank < original_rank:
        return True

    sensitive_indexes = _sensitive_line_indexes(original_dialogue)
    if sensitive_indexes & set(_actionable_line_indexes(original["issues"])):
        original_sensitive_issues = _actionable_citation_issues(_issues_for_line_indexes(original["issues"], sorted(sensitive_indexes)))
        repaired_sensitive_issues = _actionable_citation_issues(_issues_for_line_indexes(repaired["issues"], sorted(sensitive_indexes)))
        if len(repaired_sensitive_issues) < len(original_sensitive_issues):
            return True
        if _worst_citation_severity(repaired_sensitive_issues) < _worst_citation_severity(original_sensitive_issues):
            return True
    return False


def _citation_claim_preservation_ok(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]],
    repair_mode: str,
    target_line_indexes: list[int],
) -> bool:
    if repair_mode == "line_repair":
        required_original = _required_claim_ids_for_lines(original_dialogue, target_line_indexes)
        repaired_required = _required_claim_ids_for_lines(repaired_dialogue, target_line_indexes)
        return not required_original or required_original.issubset(repaired_required)

    required_original = _required_claim_ids_for_lines(
        original_dialogue,
        [idx for idx, _line in enumerate(original_dialogue)],
    )
    repaired_required = _required_claim_ids_for_lines(
        repaired_dialogue,
        [idx for idx, _line in enumerate(repaired_dialogue)],
    )
    return not required_original or required_original.issubset(repaired_required)


def _required_claim_ids_for_lines(dialogue: list[dict[str, Any]], line_indexes: list[int]) -> set[str]:
    required: set[str] = set()
    for line_index in line_indexes:
        line = _dialogue_line(dialogue, line_index)
        if line is None:
            continue
        text = str(line.get("text") or "").strip()
        if _line_is_question(text):
            continue
        claim_ids = line.get("claim_ids")
        if not isinstance(claim_ids, list):
            continue
        for claim_id in claim_ids:
            if isinstance(claim_id, str) and claim_id.strip():
                required.add(claim_id.strip())
    return required


def _line_repair_speakers_preserved(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]],
    target_line_indexes: list[int],
) -> bool:
    for line_index in target_line_indexes:
        original_line = _dialogue_line(original_dialogue, line_index)
        repaired_line = _dialogue_line(repaired_dialogue, line_index)
        if original_line is None or repaired_line is None:
            return False
        if original_line.get("speaker") != repaired_line.get("speaker"):
            return False
    return True


def _material_line_signature(line: dict[str, Any] | None) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(line, dict):
        return ("", "", ())
    claim_ids = line.get("claim_ids")
    normalized_claim_ids = tuple(
        claim_id.strip()
        for claim_id in claim_ids
        if isinstance(claim_id, str) and claim_id.strip()
    ) if isinstance(claim_ids, list) else ()
    return (
        str(line.get("speaker") or "").strip(),
        str(line.get("text") or "").strip(),
        normalized_claim_ids,
    )


def _changed_line_indexes(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]],
) -> set[int]:
    limit = max(_line_count(original_dialogue), _line_count(repaired_dialogue))
    changed: set[int] = set()
    for line_index in range(limit):
        if _material_line_signature(_dialogue_line(original_dialogue, line_index)) != _material_line_signature(
            _dialogue_line(repaired_dialogue, line_index)
        ):
            changed.add(line_index)
    return changed


def _scene_repair_preserves_unflagged_sensitive_lines(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]],
    actionable_line_indexes: set[int],
) -> bool:
    protected_indexes = _sensitive_line_indexes(original_dialogue) - set(actionable_line_indexes)
    for line_index in protected_indexes:
        if _material_line_signature(_dialogue_line(original_dialogue, line_index)) != _material_line_signature(
            _dialogue_line(repaired_dialogue, line_index)
        ):
            return False
    return True


def _relevant_reaudit_line_indexes(
    *,
    original_dialogue: list[dict[str, Any]],
    repaired_dialogue: list[dict[str, Any]],
    original_issues: list[dict[str, Any]],
    repair_mode: str,
    target_line_indexes: list[int],
) -> list[int]:
    if repair_mode == "line_repair" and target_line_indexes:
        return sorted({line_index for line_index in target_line_indexes if line_index >= 0})
    relevant = _changed_line_indexes(
        original_dialogue=original_dialogue,
        repaired_dialogue=repaired_dialogue,
    )
    relevant.update(
        line_index
        for line_index in _actionable_line_indexes(original_issues)
        if line_index >= 0
    )
    return sorted(relevant)


def _dump_citation_audit_debug(
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
        subdir="citation_audit",
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


def _dump_citation_repair_debug(
    *,
    run_id: str,
    act_number: int,
    scene_number: int,
    status: str,
    payload: dict[str, Any],
    before_dialogue: list[dict[str, Any]],
    after_dialogue: list[dict[str, Any]] | None,
    before_audit: dict[str, Any],
    after_audit: dict[str, Any] | None,
    decision: str,
    error: str | None,
    attempt: int,
    max_attempts: int,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="citation_repair",
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
            "attempt": attempt,
            "max_attempts": max_attempts,
            "input": payload,
            "before_dialogue": before_dialogue,
            "after_dialogue": after_dialogue,
            "before_audit": before_audit,
            "after_audit": after_audit,
            "error": error,
        },
    )
