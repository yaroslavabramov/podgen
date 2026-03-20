"""Phase 5.1: Showrunner — high-level episode planning."""

import json
import logging
import re
import uuid

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
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, progress

logger = logging.getLogger(__name__)

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
      "opening_question": "listener-facing question or tension that opens this act",
      "ending_turn": "what changes, lands, or becomes clearer by the end of the act",
      "focus_branch_ids": ["b1", "b3"],
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
- Plan an episode, not an outline dump. Every act must have a distinct job in the listener experience.
- Prefer a clean macro-arc: setup -> complication -> explanation -> consequence/payoff. Choose the act count that best fits the material and duration; do not default to 3 acts just because it is common.
- Start from the strongest episode-level question: why is this system strange, how does it work, and why does it matter?
- Each act should answer one part of that question and hand off naturally to the next act.
- Each claim appears in exactly ONE act (no duplication)
- Do not spread the same proposition core across multiple acts just because it is relevant in several places. Pick the best act for it.
- Avoid acts that all do the same thing. Do not make every act a generic explanation block.
- Prefer acts organized around one dominant branch or a tight pair of related branches, not a random mix of distant topics.
- DISPUTED claims MUST have a "debate moment" (conflicts >= 1 in that act's quotas)
- Act count: 2-6 based on duration and style
- Verified facts should be front-loaded in acts where audience needs grounding
- Claims with direct support and stronger source diversity should form the explanatory backbone
- Claims with illustrative evidence are good candidates for hooks, case-study moments, and act transitions
- Use claims with illustrative evidence to create vivid openings, examples, and pivots instead of stacking abstract explanation.
- Curate aggressively: each act should keep only the most important claims for its duration and role, not the full research pool
- As a rule of thumb, the curated layer should usually stay around 6-15 claims per act depending on act duration
- Last act should include takeaways and a clear "why this matters" payoff
- Titles should sound like editorial sections of a strong episode, not generic chapter labels"""


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
                    "statement": claim.statement[:200],
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

        synthetic_conflicts = _detect_numeric_conflicts(claims_summary)
        if synthetic_conflicts:
            conflicts_summary.extend(synthetic_conflicts)

        preserved_claim_ids = set(disputed_ids)
        conflict_claim_ids: set[str] = set()
        for conflict in conflicts_summary:
            conflict_ids = {
                str(claim_id).strip()
                for claim_id in (conflict.get("claim_ids", []) or [])
                if str(claim_id).strip()
            }
            conflict_claim_ids.update(conflict_ids)
            preserved_claim_ids.update(conflict_ids)

        publish_event(run_id, step_started("phase5_planning", "structure", "Creating act structure"))

        prompt_payload = {
            "duration_target_seconds": state["duration_target"],
            "show_style": bible.show_style if bible else "Conversational",
            "rigor_policy": bible.rigor_policy if bible else "",
            "claims": claims_summary,
            "conflicts": conflicts_summary,
            "disputed_claim_ids": disputed_ids,
            "duration_fill_layers": manifest.duration_fill_layers if manifest else [],
        }
        prompt = json.dumps(prompt_payload, ensure_ascii=False)
        raw = ""
        parsed_plan = None
        try:
            raw = await call_sonnet_structured(system=SHOWRUNNER_SYSTEM, prompt=prompt, temperature=0.5)
            parsed_plan = parse_llm_json(raw)
            plan = _normalize_plan(parsed_plan, state["duration_target"])
            plan = _cap_act_claim_density(
                plan,
                claims_summary,
                preserved_claim_ids=preserved_claim_ids,
                disputed_claim_ids=set(disputed_ids),
                conflict_claim_ids=conflict_claim_ids,
            )
        except Exception:
            _dump_showrunner_debug(
                run_id=run_id,
                status="llm_or_normalize_failed",
                input_payload=prompt_payload,
                llm_raw_output=raw,
                parsed_plan=parsed_plan,
                normalized_plan=None,
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


def _normalize_plan(plan, duration_target: int) -> dict:
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
                "opening_question": opening_question.strip(),
                "ending_turn": ending_turn.strip(),
                "focus_branch_ids": _string_list(act.get("focus_branch_ids")),
                "estimated_duration_seconds": _positive_int(
                    act.get("estimated_duration_seconds"),
                    default_act_duration,
                ),
                "claim_ids": _uuid_list(act.get("claim_ids")),
                "conflict_brief_ids": _uuid_list(act.get("conflict_brief_ids")),
                "content_quotas": content_quotas,
            }
        )

    if not normalized_acts:
        raise RuntimeError("Showrunner produced no valid acts")

    return {"act_count": len(normalized_acts), "acts": normalized_acts}


def _string_list(value) -> list[str]:
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


def _extract_numeric_tokens(statement: str) -> set[str]:
    return set(re.findall(r"\b\d[\d,.:/-]*\b", str(statement or "")))


def _normalize_conflict_token(token: str) -> str:
    normalized = token.lower()
    for suffix in ("ing", "ed", "es", "s"):
        if len(normalized) > 4 and normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            if len(normalized) >= 2 and normalized[-1] == normalized[-2]:
                normalized = normalized[:-1]
            break
    return normalized


def _content_tokens(statement: str) -> set[str]:
    tokens = {
        _normalize_conflict_token(token)
        for token in re.findall(r"[\w$.-]+", str(statement or "").lower())
        if len(token) > 2 and not any(ch.isdigit() for ch in token)
    }
    return tokens


def _token_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


def _detect_numeric_conflicts(claims_summary: list[dict]) -> list[dict]:
    conflicts: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, left in enumerate(claims_summary):
        left_id = str(left.get("id", "")).strip()
        left_numbers = _extract_numeric_tokens(left.get("statement", ""))
        if not left_id or not left_numbers:
            continue
        left_branches = set(left.get("branch_ids", []) or [])
        left_questions = set(left.get("question_ids", []) or [])
        left_tokens = _content_tokens(left.get("statement", ""))
        for right in claims_summary[index + 1:]:
            right_id = str(right.get("id", "")).strip()
            if not right_id:
                continue
            pair_key = tuple(sorted((left_id, right_id)))
            if pair_key in seen_pairs:
                continue
            right_numbers = _extract_numeric_tokens(right.get("statement", ""))
            if not right_numbers or left_numbers == right_numbers:
                continue
            if not (left_branches & set(right.get("branch_ids", []) or []) or left_questions & set(right.get("question_ids", []) or [])):
                continue
            overlap = _token_overlap(left_tokens, _content_tokens(right.get("statement", "")))
            if overlap < 0.15:
                continue
            seen_pairs.add(pair_key)
            conflicts.append(
                {
                    "id": f"numeric:{pair_key[0]}:{pair_key[1]}",
                    "what": (
                        "Conflicting numeric claims require resolution before writing: "
                        f"{left.get('statement', '')[:140]} | {right.get('statement', '')[:140]}"
                    ),
                    "claim_ids": [pair_key[0], pair_key[1]],
                }
            )
    return conflicts


def _claim_priority_score(claim_summary: dict) -> tuple[int, int, int, int, int]:
    verification = claim_summary.get("verification")
    verification_rank = {
        VerificationLevel.VERIFIED: 3,
        VerificationLevel.OBSERVED: 2,
        VerificationLevel.UNVERIFIED: 1,
        VerificationLevel.DISPUTED: 1,
    }.get(verification, 0)
    source_diversity = int(claim_summary.get("source_diversity", 0) or 0)
    direct_support = int(claim_summary.get("direct_support_count", 0) or 0)
    illustrative = 1 if claim_summary.get("has_illustrative") else 0
    claim_type_rank = 1 if str(claim_summary.get("type", "")).lower() in {"report", "opinion"} else 0
    return (
        verification_rank,
        min(source_diversity, 3),
        direct_support,
        illustrative,
        claim_type_rank,
    )


def _preserved_claim_priority_score(
    claim_summary: dict,
    *,
    disputed_ids: set[str],
    conflict_claim_ids: set[str],
) -> tuple[int, int, int, int, int, int]:
    claim_id = str(claim_summary.get("id", "")).strip()
    disputed_rank = 1 if claim_id in disputed_ids else 0
    conflict_rank = 1 if claim_id in conflict_claim_ids else 0
    base_score = _claim_priority_score(claim_summary)
    return (
        disputed_rank,
        conflict_rank,
        *base_score,
    )


def _max_claims_for_act(estimated_duration_seconds: int) -> int:
    return max(6, min(15, round(max(estimated_duration_seconds, 180) / 45)))


def _cap_act_claim_density(
    plan: dict,
    claims_summary: list[dict],
    *,
    preserved_claim_ids: set[str] | None = None,
    disputed_claim_ids: set[str] | None = None,
    conflict_claim_ids: set[str] | None = None,
) -> dict:
    claim_index = {str(claim["id"]): claim for claim in claims_summary if isinstance(claim, dict) and claim.get("id")}
    preserved_claim_ids = {str(claim_id).strip() for claim_id in (preserved_claim_ids or set()) if str(claim_id).strip()}
    disputed_claim_ids = {str(claim_id).strip() for claim_id in (disputed_claim_ids or set()) if str(claim_id).strip()}
    conflict_claim_ids = {str(claim_id).strip() for claim_id in (conflict_claim_ids or set()) if str(claim_id).strip()}
    capped_acts: list[dict] = []
    for act in plan.get("acts", []):
        claim_ids = [str(claim_id) for claim_id in (act.get("claim_ids", []) or []) if str(claim_id)]
        max_claims = _max_claims_for_act(int(act.get("estimated_duration_seconds", 0) or 0))
        if len(claim_ids) <= max_claims:
            capped_acts.append(act)
            continue
        preserved_in_act = [claim_id for claim_id in claim_ids if claim_id in preserved_claim_ids]
        preserved_in_act = list(dict.fromkeys(preserved_in_act))
        if len(preserved_in_act) > max_claims:
            preserved_in_act = sorted(
                preserved_in_act,
                key=lambda claim_id: (
                    _preserved_claim_priority_score(
                        claim_index.get(claim_id, {}),
                        disputed_ids=disputed_claim_ids,
                        conflict_claim_ids=conflict_claim_ids,
                    ),
                    -claim_ids.index(claim_id),
                ),
                reverse=True,
            )[:max_claims]
        ranked = sorted(
            [claim_id for claim_id in claim_ids if claim_id not in preserved_claim_ids],
            key=lambda claim_id: (
                _claim_priority_score(claim_index.get(claim_id, {})),
                -claim_ids.index(claim_id),
            ),
            reverse=True,
        )
        remaining_slots = max(0, max_claims - len(preserved_in_act))
        kept_ids = preserved_in_act + ranked[:remaining_slots]
        capped_acts.append({**act, "claim_ids": kept_ids})
    return {**plan, "acts": capped_acts}


def _dump_showrunner_debug(
    *,
    run_id: str,
    status: str,
    input_payload: dict,
    llm_raw_output: str,
    parsed_plan: object,
    normalized_plan: dict | None,
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
        },
    )
