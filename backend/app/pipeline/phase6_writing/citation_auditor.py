"""Phase 6.3: Citation audit + conditional repair."""

from __future__ import annotations

import json
import logging
import time
import uuid
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
    normalize_dialogue_output as _normalize_dialogue_output,
    set_segment_audit_finding as _set_segment_audit_finding,
)
from app.pipeline.state import PipelineState
from app.realtime.events import phase_completed, progress, quality_update, step_completed, step_started
from app.realtime.publisher import publish_event
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.workers.worker_db import worker_session_factory as async_session_factory

logger = logging.getLogger(__name__)

CITATION_SEVERITIES = {"low", "medium", "high"}
CITATION_SCOPES = {"record", "repair", "block"}
MAX_REPAIR_LINE_DRIFT_RATIO = 0.3

AUDITOR_SYSTEM = """You are a citation auditor for a podcast script.
Check each dialogue line against the claims and policies.

Return JSON:
{
  "passed": true,
  "issues": [
    {
      "line_index": 0,
      "type": "unverified_stated_as_fact|forbidden_phrase|missing_attribution|audience_mismatch|missing_claim_link|temp_source_only_claim|disputed_without_balance",
      "severity": "low|medium|high",
      "scope": "record|repair|block",
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
5. No forbidden phrases.
6. Audience level must match the show.
7. Do not require `claim_ids` on purely conversational lines that do not add a new factual assertion.
8. Questions, callbacks, recaps, scene transitions, and synthesis lines should only be flagged for `missing_claim_link` if they introduce a new unsupported fact.

Severity guide:
- `low`: acceptable for now, record it only
- `medium`: should be repaired before audio if practical
- `high`: must be repaired or block audio

Scope guide:
- `record`: informational only
- `repair`: attempt a repair pass
- `block`: if unresolved after repair, block audio"""

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
- Preserve the scene's factual meaning whenever supported by available claims.
- Add hedging or attribution where required.
- Remove or soften unsupported confident wording if needed.
- Do not add new facts, institutions, dates, figures, or claims.
- Preserve the scene's `scene_job`, `scene_type`, and content function.
- Prefer the smallest edit that fixes rigor. Do not re-author the whole scene unless necessary.
- Do not turn non-assertive lines into new exposition.
- Keep the line count close to the original scene length.
- Keep only speakers `A` and `B`.
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
                continue

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
    for cid in claim_ids:
        try:
            claim_uuid = uuid.UUID(cid)
        except (TypeError, ValueError):
            continue
        result = await db.execute(select(Claim).where(Claim.id == claim_uuid))
        claim = result.scalar_one_or_none()
        if claim is None:
            continue
        claims_map[str(claim.id)] = {
            "type": claim.claim_type,
            "verification": claim.verification_level,
            "statement": claim.statement[:200],
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
    issues = _normalize_citation_issues(audit.get("issues"))
    metrics = _normalize_metrics(audit.get("metrics"))
    passed = not any(issue.get("scope") in {"repair", "block"} for issue in issues)
    return {"passed": passed, "issues": issues, "metrics": metrics}


async def _run_citation_audit_with_retry(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    max_attempts = max(1, int(settings.max_citation_audit_attempts))
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await _run_citation_audit(prompt_payload)
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
    original_claim_ids = set(_extract_dialogue_claim_ids(original_dialogue))
    original_line_count = len(original_dialogue) if isinstance(original_dialogue, list) else 0
    max_attempts = 1
    if _has_blocking_citation_issues(original_audit["issues"]):
        max_attempts = max(1, int(settings.max_citation_repair_attempts))

    retry_feedback = ""
    for attempt in range(1, max_attempts + 1):
        prompt_payload = {
            "scene_context": scene_context,
            "original_line_count": original_line_count,
            "dialogue": original_dialogue,
            "available_claims": claims_map,
            "forbidden_phrases": forbidden_phrases,
            "rigor_policy": rigor_policy,
            "language": language,
            "issues": original_audit["issues"],
        }
        if retry_feedback:
            prompt_payload["retry_feedback"] = retry_feedback

        try:
            raw = await call_sonnet_structured(
                system=REPAIR_SYSTEM,
                prompt=json.dumps(prompt_payload, ensure_ascii=False),
                max_tokens=2048,
                temperature=0.2,
            )
            parsed = parse_llm_json(raw)
            repaired_dialogue = _normalize_dialogue_output(parsed.get("dialogue"))
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
                max_attempts=max_attempts,
            )
            if attempt >= max_attempts:
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
                max_attempts=max_attempts,
            )
            if attempt >= max_attempts:
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
        repaired_claim_ids = set(_extract_dialogue_claim_ids(repaired_dialogue))
        repaired_line_count = len(repaired_dialogue)
        accepted = _citation_repair_improved(
            original_audit,
            repaired_audit,
            original_claim_ids=original_claim_ids,
            repaired_claim_ids=repaired_claim_ids,
            original_line_count=original_line_count,
            repaired_line_count=repaired_line_count,
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
            max_attempts=max_attempts,
        )
        if accepted:
            return repaired_dialogue, repaired_audit, True
        if attempt >= max_attempts:
            return None, None, False
        retry_feedback = _build_citation_repair_retry_feedback(
            original_audit=original_audit,
            repaired_audit=repaired_audit,
        )

    return None, None, False


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

        normalized.append(
            {
                "line_index": normalized_line_index,
                "type": issue_type.strip(),
                "severity": severity,
                "scope": scope,
                "description": str(item.get("description") or "").strip(),
                "suggestion": str(item.get("suggestion") or "").strip(),
            }
        )
    return normalized


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
        int(issue.get("line_index", -1) or -1),
        str(issue.get("type") or "").strip(),
    )


def _blocking_issue_signatures(issues: list[dict[str, Any]]) -> set[tuple[int, str]]:
    return {
        _blocking_issue_signature(issue)
        for issue in issues
        if isinstance(issue, dict) and issue.get("scope") == "block"
    }


def _line_count_drift_exceeds(*, original_line_count: int, repaired_line_count: int) -> bool:
    baseline = max(original_line_count, 1)
    return abs(repaired_line_count - original_line_count) / baseline > MAX_REPAIR_LINE_DRIFT_RATIO


def _should_attempt_citation_repair(issues: list[dict[str, Any]]) -> bool:
    return any(
        issue.get("scope") in {"repair", "block"} and _citation_severity_rank(str(issue.get("severity"))) >= 2
        for issue in issues
    )


def _has_blocking_citation_issues(issues: list[dict[str, Any]]) -> bool:
    return any(issue.get("scope") == "block" for issue in issues)


def _actionable_citation_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in issues if isinstance(issue, dict) and issue.get("scope") in {"repair", "block"}]


def _summarize_citation_issues(issues: list[dict[str, Any]]) -> list[str]:
    summaries: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        scope = str(issue.get("scope") or "").strip()
        if scope not in {"repair", "block"}:
            continue
        issue_type = str(issue.get("type") or "").strip() or "issue"
        line_index = int(issue.get("line_index", -1) or -1)
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
        [issue for issue in repaired_audit.get("issues", []) if issue.get("scope") == "block"]
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


def _citation_repair_improved(
    original: dict[str, Any],
    repaired: dict[str, Any],
    *,
    original_claim_ids: set[str],
    repaired_claim_ids: set[str],
    original_line_count: int,
    repaired_line_count: int,
) -> bool:
    if original_claim_ids and not original_claim_ids.issubset(repaired_claim_ids):
        return False
    if _line_count_drift_exceeds(
        original_line_count=original_line_count,
        repaired_line_count=repaired_line_count,
    ):
        return False

    original_actionable_issues = _actionable_citation_issues(original["issues"])
    repaired_actionable_issues = _actionable_citation_issues(repaired["issues"])
    original_fact_coverage = float(original["metrics"].get("fact_coverage_ratio", 0.0) or 0.0)
    repaired_fact_coverage = float(repaired["metrics"].get("fact_coverage_ratio", 0.0) or 0.0)
    if repaired_fact_coverage + 0.05 < original_fact_coverage:
        return False

    original_blockers = _blocking_issue_signatures(original["issues"])
    repaired_blockers = _blocking_issue_signatures(repaired["issues"])
    if repaired_blockers - original_blockers:
        return False
    if original_blockers:
        return not repaired_blockers

    if repaired["passed"] and not original["passed"]:
        return True

    if len(repaired_actionable_issues) < len(original_actionable_issues):
        return True

    original_rank = _worst_citation_severity(original_actionable_issues)
    repaired_rank = _worst_citation_severity(repaired_actionable_issues)
    if repaired_rank < original_rank:
        return True
    return False


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
