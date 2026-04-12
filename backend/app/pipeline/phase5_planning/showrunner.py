"""Phase 5.1: Showrunner — high-level episode planning."""

import json
import logging
import math
import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.show_bible import ShowBible
from app.models.claim import Claim, VerificationLevel
from app.models.conflict_brief import ConflictBrief
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.phase5_planning.planning_context import load_planning_context
from app.pipeline.research_context import build_question_context_map
from app.pipeline.state import PipelineState
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.short_ids import ShortIdMap, shorten_claim_dicts, restore_id_list
from app.services.llm import call_haiku_structured, call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, progress

logger = logging.getLogger(__name__)
SHOWRUNNER_SELECTION_RETRIES = 2

SHOWRUNNER_SYSTEM = """You are a podcast showrunner planning the episode structure.
Given claims, conflicts, duration target, and show bible, create an act structure that feels like a strong episode arc, not a bucket of claims.

Return a JSON object shaped like:
{
  "act_count": <integer from 2 to 6>,
  "acts": [
    {
      "act_number": 1,
      "story_role": "setup|complicate|explain|resolve|payoff",
      "title": "Act title",
      "purpose": "one-sentence editorial function of this act",
      "internal_arc": "short phrase describing how this act moves internally",
      "opening_question": "listener-facing question or tension that opens this act",
      "ending_turn": "what changes, lands, or becomes clearer by the end of the act",
      "estimated_duration_seconds": 600,
      "claim_ids": ["id1", "id2"],
      "conflict_brief_ids": [],
      "content_quotas": {
        "verified_facts": 3,
        "attributed_opinions": 1,
        "narrative_assets": 1,
        "conflicts": 0,
        "takeaways": 1
      }
    }
  ]
}

Rules:
- Language:
  - All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language` from the user payload.
  - Do not mix languages inside one field.
- Build an episode, not a claim bucket. `episode_goal` is the user's stated intent for the podcast, and `root_question` is the best current statement of the episode-level question; use both as the editorial source of truth when deciding what deserves act time. Choose the act count (2-6) that best fits the material, duration, and style; do not default to 3 acts or compress distinct listener work just to stay compact.
- Treat `story_role` as listener work, not topic label. `setup` establishes anomaly, stakes, or the reason to care; `explain` teaches the core model; `complicate` adds context or tension that makes the simple reading less complete; `resolve` delivers the strongest test, consequence, decision, or proof; `payoff` synthesizes, reframes, compares, or lands why it matters.
- Selection comes before act assignment. First choose the smallest coherent editorial backbone that best answers `episode_goal` and `root_question`. Then decide how many distinct listener questions that selected backbone really supports, define the act jobs, and only after that assign selected claims to acts.
- Start from the strongest episode-level way into the material, not from a fixed universal arc. The best opener might be anomaly, mechanism, crisis, history, conflict, or comparison. Choose the act sequence that makes this specific episode strongest, and make sure each act has one dominant dramatic question or editorial burden.
- For each act, define a short `internal_arc` that describes movement, not topic coverage. Adjacent acts should usually have different `internal_arc` shapes, and `ending_turn` shapes should vary too.
- Use act boundaries for real shifts in listener work. A major shift in timeframe, abstraction level, or editorial function is evidence for a new act, not just a new scene. Merge candidate act material only when it would produce substantially the same `opening_question`, `internal_arc`, and `ending_turn`.
- Role purity beats compactness. Do not let `setup` fully consume `explain`, `complicate` absorb `resolve`, or `payoff` become a leftovers bin just because the episode can be compressed.
- Allocate airtime, not just claims. A strong act usually has one main listener question, one dominant mode of attention, and one main anchor mechanism, event, example, or conflict that earns most of the airtime. Prefer claims that earn airtime. In practice, prefer claims that materially answer `episode_goal` / `root_question`, change the listener's model instead of adding trivia, explain why the system works, changes, breaks, or costs what it costs, or give scene-bearing material such as an event, decision, conflict, consequence, or institutional turn.
- If an act seems to need several heavy mechanisms, several anchor events, or several kinds of listener work, treat that as a sign of overload. Move material to another act, downgrade it to support or callback material, or leave it unused instead of rescuing everything.
- Curate aggressively. Keep only claims that make a distinct editorial contribution. If multiple claims restate the same mechanism, rule, fact, event, or decision, keep the single strongest formulation and leave the rest unused unless they clearly enable a different move. Each claim appears in exactly ONE act. Do not spread the same proposition core across acts just because it feels relevant in several places. Downrank claims that mainly provide taxonomy, decorative detail, broad comparator context, repeated thesis language, or another example of a pattern the selected backbone already proves.
- Before keeping a claim in an act, ask whether removing it would force a genuinely different beat. If not, it is not fresh backbone. One act may own a mechanism or concept family, but it should stay lean rather than exhaustive.
- Explanatory acts should usually have one dominant explanatory spine. Keep extra claims only if they enable a meaningfully different move such as application, consequence, tradeoff, exception, contrast, comparison, concrete case, or real-world test.
- If one branch or theme yields more strong claims than one act can hold cleanly, keep the strongest claims in that act and leave the rest unused rather than overloading the episode. Use `total_claim_count` as an overload smell, not a quota: if one act absorbs far more claims than neighboring acts, ask whether it is trying to carry too many distinct beats or rescue too much material. Prefer a leaner act pool over exhaustive coverage.
- Ignore research-tree provenance when it does not help the editorial decision. Do not group material just because claims came from the same branch. Comparative material should stay selective; one strong outside comparator is usually enough for one beat.
- Front-load verified facts where grounding is needed. Claims with direct support and stronger source diversity should form the explanatory backbone. Claims with illustrative evidence are good candidates for hooks, case-study moments, and pivots.
- DISPUTED claims MUST have a "debate moment" (conflicts >= 1 in that act's quotas)
- The last act should include takeaways and a clear "why this matters" payoff.
- The last act should contain only claims that directly sharpen the final takeaway or reframe the episode-level question. Do not use the last act to park leftover operational facts that could be removed without changing the final takeaway.
- Titles should sound like editorial sections of a strong episode, not generic chapter labels"""

SHOWRUNNER_SELECTION_AUDIT_SYSTEM = """You are a quality checker for podcast episode act selection. Return JSON only.

You are auditing selection/design mistakes, not factual accuracy and not prose style.

Return:
{
  "passed": true,
  "issues": [
    {
      "type": "weak_claim_selected|strong_claim_omitted|act_overpacked_same_move|payoff_fresh_explainer_load|goal_misalignment",
      "severity": "low|medium|high",
      "act_number": 1,
      "selected_claim_ids": ["c1"],
      "omitted_claim_ids": ["c2"],
      "why_it_hurts": "short explanation",
      "fix_direction": "short revision direction"
    }
  ]
}

Audit only these failure modes:
- A selected claim does not earn airtime because it is mostly decorative detail, taxonomy, broad comparator context, or repeated thesis language.
- A stronger omitted claim would better serve the act or episode backbone than a weaker selected one.
- An act contains too many same-function claims (for example several mechanism claims that all do the same explanatory move).
- A payoff act still carries fresh explainer load that should have been handled earlier.
- The selected act set does not cleanly answer `episode_goal` / `root_question`.

Rules:
- Return at most 4 issues.
- Prefer medium/high issues over minor observations.
- If the plan is directionally sound, pass it.
- Do not invent omitted claims; only reference IDs present in `omitted_claims`.
- Do not request more coverage just because a claim is interesting. Flag only selection/design mistakes that materially weaken the episode."""

SHOWRUNNER_REVISION_SYSTEM = """You are an editorial planner revising an existing podcast act plan. Return JSON only.

You will receive:
- the same episode planning payload used by the showrunner,
- `current_plan`: the current act plan to revise,
- `revision_request.selection_issues`: editorial selection/design problems found by the selection audit.

Your job is to revise `current_plan`, not regenerate from scratch unless the current structure is impossible to salvage.

Return a JSON object shaped exactly like:
{
  "act_count": <integer from 2 to 6>,
  "acts": [
    {
      "act_number": 1,
      "story_role": "setup|complicate|explain|resolve|payoff",
      "title": "Act title",
      "purpose": "one-sentence editorial function of this act",
      "internal_arc": "short phrase describing how this act moves internally",
      "opening_question": "listener-facing question or tension that opens this act",
      "ending_turn": "what changes, lands, or becomes clearer by the end of the act",
      "estimated_duration_seconds": 600,
      "claim_ids": ["id1", "id2"],
      "conflict_brief_ids": [],
      "content_quotas": {
        "verified_facts": 3,
        "attributed_opinions": 1,
        "narrative_assets": 1,
        "conflicts": 0,
        "takeaways": 1
      }
    }
  ]
}

Rules:
- Treat `revision_request.selection_issues` as the highest-priority editorial instructions. If an issue names `selected_claim_ids` and `omitted_claim_ids`, prefer solving it with those claims instead of making unrelated reshuffles.
- Preserve acts, timings, and claim placements that are not implicated by the issues. Edit surgically.
- Revise the backbone first, then adjust act titles/purpose/opening/ending so the wording matches the revised claim set.
- If an issue says an act is overloaded with same-function claims, remove or replace redundant claims rather than merely rewording the act summary.
- If an issue says payoff carries fresh explainer load, move or drop that explanatory material so the payoff act becomes synthesis, not a new explainer.
- If an issue says a weaker selected claim displaced a stronger omitted one, prefer the stronger omitted one if it better serves the same act job.
- Keep each claim in exactly one act.
- Keep all human-readable fields in `output_language`.
- Do not add claims that are not in the provided `claims` list.
- If you must change act count, do so only because the issues cannot be solved cleanly within the current count."""


async def plan_episode(state: PipelineState) -> dict:
    """LangGraph node: create high-level act structure."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase5_planning", "Planning episode structure"))
    update_generation_progress(run_id, "phase5_planning", "showrunner", 50.0)

    async with async_session_factory() as db:
        # Load ShowBible
        bible = None
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()

        # Load conflict briefs
        conflicts_summary = []
        for cbid in state.get("conflict_brief_ids", []):
            result = await db.execute(select(ConflictBrief).where(ConflictBrief.id == uuid.UUID(cbid)))
            cb = result.scalar_one_or_none()
            if cb:
                conflicts_summary.append({
                    "id": str(cb.id),
                    "what": cb.what_conflicts[:200],
                    "claim_ids": [str(c) for c in cb.claim_ids],
                })

        # Load manifest for duration layers
        manifest = None
        if state.get("manifest_id"):
            result = await db.execute(select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"])))
            manifest = result.scalar_one_or_none()

        question_context_map = build_question_context_map(
            manifest.research_tree_json if manifest else None
        )
        claim_contexts, _ = await load_planning_context(
            db=db,
            claim_ids=state.get("claim_ids", []),
            generation_run_id=state["generation_run_id"],
            question_context_map=question_context_map,
        )
        claims_summary = []
        disputed_ids = []
        for cid in state.get("claim_ids", []):
            claim_summary = claim_contexts.get(cid)
            if not claim_summary:
                result = await db.execute(select(Claim).where(Claim.id == uuid.UUID(cid)))
                claim = result.scalar_one_or_none()
                if claim is None:
                    continue
                claim_summary = {
                    "id": str(claim.id),
                    "type": claim.claim_type,
                    "verification": claim.verification_level,
                    "statement": claim.statement,
                    "supporting_evidence_count": 0,
                    "illustrative_evidence_count": 0,
                    "has_illustrative": False,
                    "branch_ids": [],
                    "question_ids": [],
                    "intents": [],
                    "direct_support_count": 0,
                    "contextual_support_count": 0,
                    "source_diversity": 0,
                    "quality_hints": {
                        "has_primary_support": False,
                        "has_high_reliability_support": False,
                        "temporary_only_support": False,
                    },
                }
            claims_summary.append(claim_summary)
            if claim_summary["verification"] == VerificationLevel.DISPUTED:
                disputed_ids.append(str(claim_summary["id"]))

        publish_event(run_id, step_started("phase5_planning", "structure", "Creating act structure"))

        # Build short-ID mapping to reduce LLM token waste and typos
        all_claim_ids = [str(cs["id"]) for cs in claims_summary]
        sid = ShortIdMap(all_claim_ids)
        short_claims = shorten_claim_dicts(claims_summary, sid)
        short_disputed = sid.shorten_list(disputed_ids)
        short_conflicts = [
            {**c, "claim_ids": sid.shorten_list(c.get("claim_ids", []))}
            for c in conflicts_summary
        ]

        prompt_payload = _build_showrunner_prompt_payload(
            duration_target_seconds=state["duration_target"],
            output_language=state.get("language", "en"),
            show_style=bible.show_style if bible else "Conversational",
            episode_goal=state.get("user_prompt", ""),
            root_question=_root_question_from_research_tree(manifest.research_tree_json if manifest else None),
            claims=short_claims,
            conflicts=short_conflicts,
            disputed_claim_ids=short_disputed,
            duration_fill_layers=manifest.duration_fill_layers if manifest else [],
        )
        raw = ""
        parsed_plan = None
        selection_audit: dict[str, Any] | None = None
        revision_request: dict[str, Any] | None = None
        attempt_records: list[dict[str, Any]] = []
        current_revision_plan: dict[str, Any] | None = None
        try:
            quality_failures = 0
            while True:
                is_revision_attempt = revision_request is not None
                request_payload = (
                    _build_showrunner_revision_payload(
                        base_payload=prompt_payload,
                        current_plan=current_revision_plan if is_revision_attempt else None,
                        revision_request=revision_request,
                        sid=sid,
                    )
                    if is_revision_attempt
                    else dict(prompt_payload)
                )
                prompt = json.dumps(request_payload, ensure_ascii=False)
                raw = await call_sonnet_structured(
                    system=SHOWRUNNER_REVISION_SYSTEM if is_revision_attempt else SHOWRUNNER_SYSTEM,
                    prompt=prompt,
                    temperature=0.5,
                )
                parsed_plan = parse_llm_json(raw)
                candidate_plan = _normalize_plan(parsed_plan, state["duration_target"], sid=sid)
                candidate_plan = _rebalance_act_claim_distribution(candidate_plan, claim_lookup={claim["id"]: claim for claim in claims_summary})
                candidate_plan = _log_act_claim_counts(candidate_plan)
                candidate_selection_audit = await _run_showrunner_selection_audit(
                    prompt_payload=prompt_payload,
                    plan=candidate_plan,
                    sid=sid,
                )
                blocker_issues = _showrunner_selection_blockers(candidate_selection_audit)
                attempt_records.append(
                    {
                        "attempt_number": len(attempt_records) + 1,
                        "mode": "revision" if is_revision_attempt else "initial",
                        "request_payload": request_payload,
                        "llm_output": {
                            "raw_chars": len(raw or ""),
                            "raw": raw,
                            "parsed_type": type(parsed_plan).__name__ if parsed_plan is not None else None,
                            "parsed": parsed_plan,
                        },
                        "normalized_plan": candidate_plan,
                        "selection_audit": candidate_selection_audit,
                        "blocker_issues": blocker_issues,
                        "accepted": not blocker_issues,
                    }
                )
                if not blocker_issues:
                    plan = candidate_plan
                    selection_audit = candidate_selection_audit
                    break
                if quality_failures >= SHOWRUNNER_SELECTION_RETRIES:
                    plan = candidate_plan
                    selection_audit = candidate_selection_audit
                    break
                quality_failures += 1
                current_revision_plan = candidate_plan
                revision_request = {
                    "message": (
                        "Previous plan had editorial selection/design problems. "
                        "Revise the current plan instead of regenerating from scratch. "
                        "Fix the listed selection issues directly."
                    ),
                    "selection_issues": blocker_issues,
                }
        except Exception:
            attempt_records.append(
                {
                    "attempt_number": len(attempt_records) + 1,
                    "mode": "revision" if revision_request is not None else "initial",
                    "request_payload": request_payload if "request_payload" in locals() else None,
                    "llm_output": {
                        "raw_chars": len(raw or ""),
                        "raw": raw,
                        "parsed_type": type(parsed_plan).__name__ if parsed_plan is not None else None,
                        "parsed": parsed_plan,
                    },
                    "normalized_plan": None,
                    "selection_audit": selection_audit,
                    "blocker_issues": _showrunner_selection_blockers(selection_audit),
                    "accepted": False,
                }
            )
            _dump_showrunner_debug(
                run_id=run_id,
                status="llm_or_normalize_failed",
                input_payload=prompt_payload,
                llm_raw_output=raw,
                parsed_plan=parsed_plan,
                normalized_plan=None,
                selection_audit=selection_audit,
                attempts=attempt_records,
            )
            raise

        # Validate: all disputed claims have debate slots
        for act in plan.get("acts", []):
            act_disputed = [c for c in act.get("claim_ids", []) if c in disputed_ids]
            act_conflicts = [
                conflict
                for conflict in conflicts_summary
                if set(conflict.get("claim_ids", [])) & set(act.get("claim_ids", []))
            ]
            if (act_disputed or act_conflicts) and act.get("content_quotas", {}).get("conflicts", 0) < 1:
                act.setdefault("content_quotas", {})["conflicts"] = max(1, len(act_disputed) or len(act_conflicts))

        # Update manifest
        if manifest:
            manifest.act_count = plan.get("act_count", len(plan.get("acts", [])))
            manifest.acts_json = plan.get("acts", [])
            await db.commit()

    _dump_showrunner_debug(
        run_id=run_id,
        status="ok",
        input_payload=prompt_payload,
        llm_raw_output=raw,
        parsed_plan=parsed_plan,
        normalized_plan=plan,
        selection_audit=selection_audit,
        attempts=attempt_records,
    )

    publish_event(run_id, step_completed("phase5_planning", "structure"))
    publish_event(run_id, progress(55.0, f"Planned {plan.get('act_count', 0)} acts"))

    return {"manifest_id": state.get("manifest_id", ""), "current_phase": "phase5_planning"}


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _build_showrunner_prompt_payload(
    *,
    duration_target_seconds: int,
    output_language: str,
    show_style: str,
    episode_goal: str,
    root_question: str,
    claims: list[dict],
    conflicts: list[dict],
    disputed_claim_ids: list[str],
    duration_fill_layers: list | None,
) -> dict:
    sanitized_claims: list[dict] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        copy = dict(claim)
        copy.pop("branch_ids", None)
        copy.pop("question_ids", None)
        sanitized_claims.append(copy)

    return {
        "duration_target_seconds": duration_target_seconds,
        "output_language": str(output_language or "en").strip() or "en",
        "show_style": show_style,
        "episode_goal": str(episode_goal or "").strip(),
        "root_question": str(root_question or "").strip(),
        "total_claim_count": len(sanitized_claims),
        "claims": sanitized_claims,
        "conflicts": conflicts,
        "disputed_claim_ids": disputed_claim_ids,
        "duration_fill_layers": duration_fill_layers or [],
    }


def _build_showrunner_revision_payload(
    *,
    base_payload: dict[str, Any],
    current_plan: dict | None,
    revision_request: dict[str, Any] | None,
    sid: ShortIdMap | None,
) -> dict[str, Any]:
    payload = dict(base_payload)
    if current_plan is not None:
        payload["current_plan"] = _shorten_plan_claim_ids(current_plan, sid=sid)
    if revision_request:
        payload["revision_request"] = revision_request
    return payload


def _root_question_from_research_tree(research_tree_json: dict | None) -> str:
    if not isinstance(research_tree_json, dict):
        return ""
    root_question = research_tree_json.get("root_question")
    if not isinstance(root_question, str):
        return ""
    return root_question.strip()


def _selected_claim_ids(plan: dict) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for act in plan.get("acts", []):
        if not isinstance(act, dict):
            continue
        for claim_id in _id_list(act.get("claim_ids")):
            if claim_id in seen:
                continue
            seen.add(claim_id)
            selected.append(claim_id)
    return selected


def _compact_selection_claim_view(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": claim.get("id"),
        "statement": str(claim.get("statement") or ""),
        "type": str(claim.get("type") or ""),
        "verification": str(claim.get("verification") or ""),
        "intents": list(claim.get("intents") or []),
        "has_illustrative": bool(claim.get("has_illustrative")),
        "direct_support_count": int(claim.get("direct_support_count") or 0),
        "source_diversity": int(claim.get("source_diversity") or 0),
    }


def _compact_selection_act_view(plan: dict, claim_lookup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    acts_view: list[dict[str, Any]] = []
    for act in plan.get("acts", []):
        if not isinstance(act, dict):
            continue
        claim_ids = _id_list(act.get("claim_ids"))
        acts_view.append(
            {
                "act_number": _positive_int(act.get("act_number"), 0),
                "story_role": str(act.get("story_role") or ""),
                "title": str(act.get("title") or ""),
                "purpose": str(act.get("purpose") or ""),
                "internal_arc": str(act.get("internal_arc") or ""),
                "opening_question": str(act.get("opening_question") or ""),
                "ending_turn": str(act.get("ending_turn") or ""),
                "estimated_duration_seconds": _positive_int(act.get("estimated_duration_seconds"), 0),
                "claim_ids": claim_ids,
                "claims": [
                    _compact_selection_claim_view(claim_lookup[claim_id])
                    for claim_id in claim_ids
                    if claim_id in claim_lookup
                ],
            }
        )
    return acts_view


def _normalize_showrunner_selection_issues(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed_types = {
        "weak_claim_selected",
        "strong_claim_omitted",
        "act_overpacked_same_move",
        "payoff_fresh_explainer_load",
        "goal_misalignment",
    }
    allowed_severities = {"low", "medium", "high"}
    normalized: list[dict[str, Any]] = []
    for raw_issue in value:
        if not isinstance(raw_issue, dict):
            continue
        issue_type = str(raw_issue.get("type") or "").strip()
        severity = str(raw_issue.get("severity") or "").strip().lower()
        if issue_type not in allowed_types or severity not in allowed_severities:
            continue
        issue: dict[str, Any] = {
            "type": issue_type,
            "severity": severity,
            "why_it_hurts": str(raw_issue.get("why_it_hurts") or "").strip(),
            "fix_direction": str(raw_issue.get("fix_direction") or "").strip(),
        }
        act_number = raw_issue.get("act_number")
        if isinstance(act_number, int) and act_number > 0:
            issue["act_number"] = act_number
        selected_claim_ids = [item for item in raw_issue.get("selected_claim_ids", []) if isinstance(item, str)]
        omitted_claim_ids = [item for item in raw_issue.get("omitted_claim_ids", []) if isinstance(item, str)]
        if selected_claim_ids:
            issue["selected_claim_ids"] = selected_claim_ids
        if omitted_claim_ids:
            issue["omitted_claim_ids"] = omitted_claim_ids
        normalized.append(issue)
        if len(normalized) >= 4:
            break
    return normalized


def _validate_showrunner_selection_issue_ids(
    issues: list[dict[str, Any]],
    *,
    selected_ids: set[str],
    omitted_ids: set[str],
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for issue in issues:
        issue_type = issue.get("type")
        selected_claim_ids = [
            claim_id
            for claim_id in issue.get("selected_claim_ids", [])
            if isinstance(claim_id, str) and claim_id in selected_ids
        ]
        omitted_claim_ids = [
            claim_id
            for claim_id in issue.get("omitted_claim_ids", [])
            if isinstance(claim_id, str) and claim_id in omitted_ids
        ]
        if issue_type in {"weak_claim_selected", "act_overpacked_same_move", "payoff_fresh_explainer_load"} and not selected_claim_ids:
            continue
        if issue_type == "strong_claim_omitted" and not omitted_claim_ids:
            continue
        cleaned = dict(issue)
        if selected_claim_ids:
            cleaned["selected_claim_ids"] = selected_claim_ids
        else:
            cleaned.pop("selected_claim_ids", None)
        if omitted_claim_ids:
            cleaned["omitted_claim_ids"] = omitted_claim_ids
        else:
            cleaned.pop("omitted_claim_ids", None)
        validated.append(cleaned)
    return validated


async def _run_showrunner_selection_audit(
    *,
    prompt_payload: dict[str, Any],
    plan: dict,
    sid: ShortIdMap | None = None,
) -> dict[str, Any]:
    audit_plan = _shorten_plan_for_selection_audit(plan, sid=sid)
    claim_lookup = {
        claim["id"]: claim
        for claim in prompt_payload.get("claims", [])
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    }
    selected_ids = set(_selected_claim_ids(audit_plan))
    omitted_ids = set(claim_lookup) - selected_ids
    omitted_claims = [
        _compact_selection_claim_view(claim)
        for claim_id, claim in claim_lookup.items()
        if claim_id in omitted_ids
    ]
    audit_prompt = json.dumps(
        {
            "episode_goal": prompt_payload.get("episode_goal", ""),
            "root_question": prompt_payload.get("root_question", ""),
            "acts": _compact_selection_act_view(audit_plan, claim_lookup),
            "omitted_claims": omitted_claims,
            "task": (
                "Audit the selected backbone for editorial selection/design mistakes only. "
                "Prefer medium/high issues. If the plan is directionally sound, pass it."
            ),
        },
        ensure_ascii=False,
    )
    raw = ""
    try:
        raw = await call_haiku_structured(
            system=SHOWRUNNER_SELECTION_AUDIT_SYSTEM,
            prompt=audit_prompt,
            max_tokens=2048,
        )
        parsed = parse_llm_json(raw)
    except Exception as exc:
        logger.warning(
            "Showrunner selection audit failed after a valid candidate plan; accepting candidate without selection retry: %s",
            exc,
        )
        return {
            "passed": True,
            "issues": [],
            "audit_failed": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "raw": raw,
        }
    issues = _validate_showrunner_selection_issue_ids(
        _normalize_showrunner_selection_issues(parsed.get("issues") if isinstance(parsed, dict) else None),
        selected_ids=selected_ids,
        omitted_ids=omitted_ids,
    )
    passed = (bool(parsed.get("passed", not issues)) if isinstance(parsed, dict) else not issues) or not issues
    return {"passed": passed and not _showrunner_selection_blockers({"issues": issues}), "issues": issues}


def _showrunner_selection_blockers(audit: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(audit, dict):
        return []
    issues = audit.get("issues")
    if not isinstance(issues, list):
        return []
    blockers: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity") in {"medium", "high"}:
            blockers.append(issue)
    return blockers


def _shorten_plan_claim_ids(plan: dict, *, sid: ShortIdMap | None) -> dict:
    if sid is None:
        return plan
    acts_raw = plan.get("acts")
    if not isinstance(acts_raw, list):
        return plan
    shortened_plan = dict(plan)
    shortened_plan["acts"] = [
        {
            **act,
            "claim_ids": sid.shorten_list(_uuid_list(act.get("claim_ids"))),
        }
        for act in acts_raw
        if isinstance(act, dict)
    ]
    return shortened_plan


def _shorten_plan_for_selection_audit(plan: dict, *, sid: ShortIdMap | None) -> dict:
    return _shorten_plan_claim_ids(plan, sid=sid)


def _id_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _uuid_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            uuid.UUID(item)
        except ValueError:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _normalize_plan(plan, duration_target: int, *, sid: ShortIdMap | None = None) -> dict:
    if isinstance(plan, dict):
        acts_raw = plan.get("acts")
    elif isinstance(plan, list):
        acts_raw = plan
    else:
        acts_raw = None

    if not isinstance(acts_raw, list):
        raise RuntimeError("Showrunner response does not contain an acts list")

    default_act_duration = max(180, duration_target // max(1, len(acts_raw)))
    normalized_acts: list[dict] = []

    for i, act in enumerate(acts_raw, start=1):
        if not isinstance(act, dict):
            continue
        act_number = _positive_int(act.get("act_number"), i)
        title = act.get("title")
        if not isinstance(title, str) or not title.strip():
            title = f"Act {act_number}"

        content_quotas = act.get("content_quotas")
        if not isinstance(content_quotas, dict):
            content_quotas = {}

        story_role = act.get("story_role")
        if not isinstance(story_role, str) or not story_role.strip():
            story_role = "explain"

        purpose = act.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            purpose = title.strip()

        internal_arc = act.get("internal_arc")
        if not isinstance(internal_arc, str):
            internal_arc = ""

        opening_question = act.get("opening_question")
        if not isinstance(opening_question, str):
            opening_question = ""

        ending_turn = act.get("ending_turn")
        if not isinstance(ending_turn, str):
            ending_turn = ""

        normalized_acts.append(
            {
                "act_number": act_number,
                "story_role": story_role.strip(),
                "title": title.strip(),
                "purpose": purpose.strip(),
                "internal_arc": internal_arc.strip(),
                "opening_question": opening_question.strip(),
                "ending_turn": ending_turn.strip(),
                "estimated_duration_seconds": _positive_int(
                    act.get("estimated_duration_seconds"),
                    default_act_duration,
                ),
                "claim_ids": restore_id_list(act.get("claim_ids"), sid) if sid else _uuid_list(act.get("claim_ids")),
                "conflict_brief_ids": _uuid_list(act.get("conflict_brief_ids")),
                "content_quotas": content_quotas,
            }
        )

    if not normalized_acts:
        raise RuntimeError("Showrunner produced no valid acts")

    return {"act_count": len(normalized_acts), "acts": normalized_acts}
def _log_act_claim_counts(plan: dict) -> dict:
    """Log claim counts per act for diagnostics (no filtering)."""
    for act in plan.get("acts", []):
        act_num = act.get("act_number", "?")
        n_claims = len(act.get("claim_ids", []) or [])
        dur = act.get("estimated_duration_seconds", 0)
        logger.info("Showrunner act %s: %d claims, %ds", act_num, n_claims, dur)
    return plan


def _compute_max_claims_per_act(total_claim_count: int, act_count: int) -> int:
    if total_claim_count <= 0 or act_count <= 0:
        return 0
    return min(total_claim_count, max(8, math.ceil((total_claim_count / act_count) * 1.5)))


def _rebalance_act_claim_distribution(
    plan: dict,
    *,
    claim_lookup: dict[str, dict],
) -> dict:
    acts_raw = plan.get("acts")
    if not isinstance(acts_raw, list) or not acts_raw:
        return plan

    acts: list[dict[str, object]] = [act for act in acts_raw if isinstance(act, dict)]
    if not acts:
        return plan

    seen_claim_ids: set[str] = set()
    for idx, act in enumerate(acts):
        deduped_claim_ids: list[str] = []
        for claim_id in _uuid_list(act.get("claim_ids")):
            if claim_id in seen_claim_ids:
                logger.warning(
                    "Showrunner duplicated claim %s across acts; keeping first occurrence and dropping from act %s",
                    claim_id,
                    _positive_int(act.get("act_number"), idx + 1),
                )
                continue
            seen_claim_ids.add(claim_id)
            deduped_claim_ids.append(claim_id)
        act["claim_ids"] = deduped_claim_ids

    assigned_claim_count = sum(len(_uuid_list(act.get("claim_ids"))) for act in acts)
    max_claims_per_act = _compute_max_claims_per_act(assigned_claim_count, len(acts))
    if max_claims_per_act <= 0:
        return plan

    for idx, act in enumerate(acts):
        claim_ids = _uuid_list(act.get("claim_ids"))
        act_number = _positive_int(act.get("act_number"), idx + 1)
        act["claim_ids"] = claim_ids
        if len(claim_ids) > max_claims_per_act:
            logger.warning(
                "Showrunner overloaded act %s with %d claims (soft ceiling %d); leaving act unchanged to preserve semantic intent",
                act_number,
                len(claim_ids),
                max_claims_per_act,
            )

    plan["acts"] = acts
    return plan


def _dump_showrunner_debug(
    *,
    run_id: str,
    status: str,
    input_payload: dict,
    llm_raw_output: str,
    parsed_plan: object,
    normalized_plan: dict | None,
    selection_audit: dict[str, Any] | None = None,
    attempts: list[dict[str, Any]] | None = None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="planning_showrunner",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "status": status,
            "input": input_payload,
            "llm_output": {
                "raw_chars": len(llm_raw_output or ""),
                "raw": llm_raw_output,
                "parsed_type": type(parsed_plan).__name__ if parsed_plan is not None else None,
                "parsed": parsed_plan,
            },
            "normalized_plan": normalized_plan,
            "selection_audit": selection_audit,
            "attempts": attempts or [],
        },
    )
