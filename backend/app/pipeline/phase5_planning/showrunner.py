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
from app.services.llm import call_haiku_structured, call_openai_structured, call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, progress

logger = logging.getLogger(__name__)
SHOWRUNNER_SELECTION_RETRIES = 2
SHOWRUNNER_SELECTION_AUDIT_MAX_ISSUES = 4
SHOWRUNNER_SELECTION_AUDIT_MAX_TOKENS = 4096
SHOWRUNNER_ACT_CLAIM_ROLE_FIELDS = ("backbone_claim_ids", "support_claim_ids", "callback_claim_ids")
SHOWRUNNER_OPENAI_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
SHOWRUNNER_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "act_count": {"type": "integer"},
        "acts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "act_number": {"type": "integer"},
                    "story_role": {"type": "string", "enum": ["setup", "complicate", "explain", "resolve", "payoff"]},
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "act_job": {"type": "string"},
                    "internal_arc": {"type": "string"},
                    "opening_question": {"type": "string"},
                    "ending_turn": {"type": "string"},
                    "estimated_duration_seconds": {"type": "integer"},
                    "backbone_claim_ids": {"type": "array", "items": {"type": "string"}},
                    "support_claim_ids": {"type": "array", "items": {"type": "string"}},
                    "callback_claim_ids": {"type": "array", "items": {"type": "string"}},
                    "conflict_brief_ids": {"type": "array", "items": {"type": "string"}},
                    "content_quotas": {
                        "type": "object",
                        "properties": {
                            "verified_facts": {"type": "integer"},
                            "attributed_opinions": {"type": "integer"},
                            "narrative_assets": {"type": "integer"},
                            "conflicts": {"type": "integer"},
                            "takeaways": {"type": "integer"},
                        },
                        "required": [
                            "verified_facts",
                            "attributed_opinions",
                            "narrative_assets",
                            "conflicts",
                            "takeaways",
                        ],
                        "additionalProperties": False,
                    },
                    "why_this_is_enough": {"type": "string"},
                },
                "required": [
                    "act_number",
                    "story_role",
                    "title",
                    "purpose",
                    "act_job",
                    "internal_arc",
                    "opening_question",
                    "ending_turn",
                    "estimated_duration_seconds",
                    "backbone_claim_ids",
                    "support_claim_ids",
                    "callback_claim_ids",
                    "conflict_brief_ids",
                    "content_quotas",
                    "why_this_is_enough",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["act_count", "acts"],
    "additionalProperties": False,
}


SHOWRUNNER_SYSTEM = """You are a podcast showrunner planning the episode structure.
Create a strong episode arc, not a bucket of claims.

Return a JSON object shaped like:
{
  "act_count": <integer from 2 to 6>,
  "acts": [
    {
      "act_number": 1,
      "story_role": "setup|complicate|explain|resolve|payoff",
      "title": "Act title",
      "purpose": "one-sentence editorial function of this act",
      "act_job": "short concrete editorial job this act performs",
      "internal_arc": "short phrase describing how this act moves internally",
      "opening_question": "listener-facing question or tension that opens this act",
      "ending_turn": "what changes, lands, or becomes clearer by the end of the act",
      "estimated_duration_seconds": 600,
      "backbone_claim_ids": ["id1"],
      "support_claim_ids": ["id2"],
      "callback_claim_ids": ["id3"],
      "conflict_brief_ids": [],
      "content_quotas": {
        "verified_facts": 3,
        "attributed_opinions": 1,
        "narrative_assets": 1,
        "conflicts": 0,
        "takeaways": 1
      },
      "why_this_is_enough": "why this act is complete without adding more selected claims"
    }
  ]
}

Rules:
- Language:
  - All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language` from the user payload.
  - Do not mix languages inside one field.
- Build an episode, not a claim bucket. `episode_goal` is the user's stated intent, and `root_question` is the episode-level question; use both to decide what deserves time. Choose 2-6 acts; do not default to 3 acts or compress distinct listener work just to stay compact.
- Treat `story_role` as listener work, not topic label or fixed position. The first act can be a mechanism, crisis, conflict, decision, or historical turn if that creates stronger momentum. `setup` establishes anomaly/stakes; `explain` teaches the model; `complicate` deepens the simple reading; `resolve` delivers the strongest test/consequence/decision/proof; `payoff` synthesizes why it matters.
- Selection comes before act assignment. First choose the smallest coherent editorial backbone that best answers `episode_goal` and `root_question`; then decide how many listener questions it supports, define act jobs, and only then assign selected claims.
  - Do not start by arranging every available claim. Start by imagining the episode pitch to an editor: "the listener comes in thinking X; after these acts, they now understand Y." A claim that would not help that listener transformation is rarely backbone material.
  - Treat each act's `title`, `act_job`, `opening_question`, and `ending_turn` as a contract. Backbone claims must directly pay that contract; support/callback claims may clarify it but cannot carry the promised answer.
  - For every act, split selected material by airtime role: `backbone_claim_ids` are the claims that scenes should be built around; `support_claim_ids` may be used inside those scenes but should not create scenes by themselves; `callback_claim_ids` may return as context only. Do not output a combined `claim_ids` list; pipeline derives it.
  - Match question type to backbone. If the act asks why, backbone needs cause/choice/constraint/incentive/rationale; if it asks how, backbone needs mechanism/process; if it asks so-what, backbone needs consequence/meaning; if it asks what happened, backbone may use event/chronology. Promote the claim that answers the contract or rewrite the act question/job.
- Start from the strongest episode-level way into the material, not from a fixed universal arc. Compare possible centers of gravity as options, not templates: contradiction, origin, mechanism, crisis/test, decision, conflict, consequence, comparison, or payoff. Any order can work if act contracts and backbone claims make the listener transformation clear. Do not default to present-anomaly -> chronology -> mechanism -> payoff.
- For each act, define a short `internal_arc` that describes movement, not topic coverage. Adjacent acts should usually have different `internal_arc` and `ending_turn` shapes.
- Use act boundaries for real shifts in listener work. A major shift in timeframe, abstraction level, or editorial function can justify a new act. Merge material only when it would produce substantially the same `opening_question`, `internal_arc`, and `ending_turn`.
- Role purity beats compactness. Do not let `setup` fully consume `explain`, `complicate` absorb `resolve`, or `payoff` become a leftovers bin just because the episode can be compressed.
  - For setup/hook work, pose the paradox; backbone should carry visible anomaly/stakes, not the answer. Do not make answer-revealing authority, cause, outcome, or mechanism claims backbone unless that act teaches that answer. Foreshadow at most as support/callback so later explain/resolve acts still land the reveal.
- Treat airtime as editorial investment. Every minute must buy stronger care, a mental model, consequential event/decision/test, reversal/complication, or payoff that changes the listener's answer.
  - Set `estimated_duration_seconds` from earned listener work, not equal blocks. Lightweight hook, recognition, or setup should be a short launchpad; do not pad it. Give surplus to the hardest mechanism, crisis/test, decision, or payoff.
  - Pure setup/hook work usually earns 90-180 seconds, not a full explainer act. Exceed that only when setup carries a real event, decision, conflict, or mechanism.
  - If an act mainly buys recognition, inventory, texture, taxonomy, generic background, or "more examples," compress it into support for a stronger act.
  - A strong act has one main listener question, one dominant mode of attention, and one anchor mechanism, event, example, or conflict that earns most airtime.
  - A backbone claim earns airtime only if the listener needs time to understand it, feel its consequence, or use it to answer the episode question. Good backbone introduces a necessary mechanism, real decision, crisis/test, cost/tradeoff, consequence, or irreversible turn.
  - A claim is weak backbone when it mainly lists surface details, names categories, repeats an already-landed thesis, gives a second example of the same move, or supplies broad comparator context without changing the listener's model. Such claims can be hooks, support, or callbacks.
  - Vivid detail is an entrance, not a destination. Use it to open curiosity, then move to the pressure, mechanism, choice, or consequence.
  - Dense mechanisms, crises, and consequential decisions need more oxygen than color, taxonomy, or scene-setting. If the episode contains a hard mechanism or high-stakes test, protect time for that before giving time to decorative setup.
  - Never require stress-tests by default; use a present, strongly relevant proof-under-pressure case only when it beats weaker support/comparator/context.
- If an act seems to need several heavy mechanisms, several anchor events, or several kinds of listener work, treat that as a sign of overload. Move material to another act, downgrade it to support or callback material, or leave it unused instead of rescuing everything.
- Curate aggressively. Keep only claims with a distinct editorial contribution. If multiple claims restate the same mechanism, rule, fact, event, or decision, keep the strongest unless another enables a different move. Each claim appears in exactly ONE act.
- Before keeping a claim in an act, ask whether removing it would force a genuinely different beat. If not, it is not fresh backbone. One act may own a mechanism or concept family, but it should stay lean rather than exhaustive.
  - Keep each act backbone lean enough to produce clear scenes, usually 3-7 claims. Decorative hooks, comparators, repeated mechanism details, and already-landed thesis claims should be support/callback or unused, not backbone.
  - Before final JSON, silently verify each act promise, backbone payment, context-only claims, and premature setup reveals.
- Explanatory acts should usually have one dominant explanatory spine. Keep extra claims only for a different move: application, consequence, tradeoff, exception, contrast, comparison, case, or real-world test.
- Use `total_claim_count` as an overload smell, not a quota. If one act absorbs far more claims than neighboring acts, ask whether it is carrying too many distinct beats or rescuing too much material. Prefer a leaner act pool over exhaustive coverage.
- Ignore research-tree provenance when it does not help the editorial decision. Do not group material just because claims came from the same branch. Comparative material should stay selective.
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
  "rationale": "why the plan passed or failed, including the strongest omitted alternative considered",
  "issues": [
    {
      "severity": "low|medium|high",
      "scope": "episode|act|claim_role|claim",
      "revision_mode": "local_edit|act_rebuild|episode_rebuild",
      "act_number": 1,
      "diagnosis": "short explanation of the editorial problem",
      "minimal_backbone_claim_ids": ["c1"],
      "drop_or_demote_claim_ids": ["c2"],
      "add_or_promote_claim_ids": ["c3"],
      "support_or_callback_claim_ids": ["c4"],
      "fix_direction": "short revision direction"
    }
  ]
}

Audit only material selection/design failures:
- `episode` scope: the whole act sequence fails the episode goal, has too few/many acts for the listener work, or uses the wrong macro-arc.
- `act` scope: one act combines incompatible jobs, has the wrong act job, lacks a minimal spine, or carries fresh explainer load in payoff.
- `claim_role` scope: a claim is in the wrong airtime role, such as support/callback material promoted to backbone, or support material that would demand its own scene.
- `claim` scope: a selected claim should be dropped/demoted, or an omitted claim should be added/promoted because it better serves the stated act job.

Audit the same airtime-worthiness standard the showrunner used:
- Act-contract first check: for each act, read `title`, `act_job`, `opening_question`, and `ending_turn` as the promise. The backbone must be able to pay that promise without unsupported inference from support/callback.
- If an issue is medium/high, diagnose the broken act contract: what the act promises, why current backbone fails to pay it, and why the fix is not just a richer optional alternative.
- Setup/hook check: if an act mainly poses anomaly/recognition and not explanation, are answer-revealing authority/cause/outcome/mechanism claims kept out of backbone so the act opens the question instead of solving it?
- Causal-claim anti-pendulum: do not call a causal/rationale claim premature when it directly answers the current `opening_question`/`act_job`. It is premature only when it answers a later act's listener question and is not needed by the current act; if demoting it, name what still answers the current question.
- Does each backbone claim earn act-level time by changing the listener's model, carrying a necessary mechanism, decision, crisis/test, cost/tradeoff, consequence, or irreversible turn?
- Would the act still work if a backbone claim were removed? If yes, that claim is probably support/callback, not backbone.
- Does `estimated_duration_seconds` match the act's actual listener work, or is a lightweight hook/setup/bridge being padded while a harder mechanism, crisis/test, decision, or payoff is underfunded?
- If setup/hook mainly poses an anomaly or recognition beat, is it kept near 90-180 seconds, or is it overfunded without a real event, decision, conflict, or mechanism?
- Does a setup/hook act prematurely put answer-revealing authority/cause/outcome/mechanism claims in backbone, mini-solving the episode question before a later explain/resolve act can land the reveal?
- Is vivid detail only an entrance into a larger tension, or has the plan made decorative setup the destination?
- Do dense mechanisms, crises, and consequential decisions get enough oxygen relative to color, taxonomy, broad comparators, or scene-setting?
- Is the plan coverage-first, trying to rescue all interesting claims, instead of choosing the smallest spine that answers `episode_goal` / `root_question`?
- Compare selected backbone claims against omitted claims as opportunity cost. If an omitted mechanism, decision, crisis/test, consequence, or reversal would buy a stronger listener transformation than selected inventory/context, flag the tradeoff.
- Do not require a stress-test/case-study beat for every topic. Only flag omitted proof-under-pressure material when claims actually contain it, it is strongly relevant to `episode_goal`/`root_question`, the plan lacks an equivalent test, and it would materially improve the listener transformation over selected support/comparator/context.
- Match question type to proof type: if an act asks why/what allowed/why persisted, backbone needs cause/choice/constraint/rationale; if it asks how, backbone needs mechanism/process; if it asks so-what, backbone needs consequence/meaning; if it asks what happened, event/chronology may be enough. Flag only when proof type and act promise do not match.
- For hard mechanisms, avoid treating zero-direct-support claims as backbone when stronger supported claims can carry the same beat. Use weakly supported mechanism claims as support unless they are the only claim that makes the listener turn possible.

Rules:
- Return at most 4 issues.
- Return compact JSON only: no Markdown fences, rationale <= 90 words, each diagnosis/fix_direction <= 280 characters.
- Always include `rationale`. If passing, briefly name the strongest omitted opportunity-cost candidate you considered and why it is optional, or say no omitted claim would materially change the listener transformation.
- Pass when the selected act spine performs the stated listener work and remaining alternatives are optional improvements, polish, extra color, or marginal strengthening.
- Use `medium` or `high` only for blockers that leave a stated act job, opening question, ending turn, or episode transformation materially unsupported, overloaded, or misleading. Use `low` for non-blocking improvements, or pass and mention them in `rationale`.
- Each issue must first name the broken listener-work invariant: which `act_job`, `opening_question`, `internal_arc`, `ending_turn`, or episode transformation fails. Claim IDs are evidence for that diagnosis, not the diagnosis itself.
- Do not raise medium/high if you cannot explain why the current backbone fails the act contract. A better, more vivid, or more complete alternative is low severity unless the promised listener transformation is currently unsupported.
- Do not ask for claim swaps just because an omitted claim is interesting, more concrete, or source-rich. Add/promote an omitted claim only when the current backbone cannot perform the stated listener work without it.
- Do not prescribe a universal story order. If you flag sequencing, diagnose the broken listener question, missing turn, or unsupported act job; do not fail a plan merely because it puts event before mechanism, mechanism before event, chronology before payoff, or payoff before history.
- Do not invent claim IDs. Use only IDs present in the current acts or in `omitted_claims`.
- For mechanism/history/crisis/payoff issues, include `minimal_backbone_claim_ids` when you can name the smallest viable set of claims needed for the act to work.
- Audit `backbone_claim_ids` as the real act spine. Do not penalize a decorative or comparator claim merely for existing as support/callback unless it would still demand fresh explanation.
- Each selected claim object includes `role`; verify requested promote/demote fixes against the current role before failing the plan.
- Resolve tradeoffs inside the audit: do not ask to remove a claim as redundant and then require it as missing support in the same response. Prefer one minimal spine plus optional drops over contradictory claim swaps.
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
      "act_job": "short concrete editorial job this act performs",
      "internal_arc": "short phrase describing how this act moves internally",
      "opening_question": "listener-facing question or tension that opens this act",
      "ending_turn": "what changes, lands, or becomes clearer by the end of the act",
      "estimated_duration_seconds": 600,
      "backbone_claim_ids": ["id1"],
      "support_claim_ids": ["id2"],
      "callback_claim_ids": ["id3"],
      "conflict_brief_ids": [],
      "content_quotas": {
        "verified_facts": 3,
        "attributed_opinions": 1,
        "narrative_assets": 1,
        "conflicts": 0,
        "takeaways": 1
      },
      "why_this_is_enough": "why this act is complete without adding more selected claims"
    }
  ]
}

Rules:
- Treat `revision_request.selection_issues` as the highest-priority editorial diagnosis, but resolve tradeoffs across issues instead of obeying each claim list mechanically. Do not fix one issue by recreating another.
- Do not output a combined `claim_ids` list; revise only `backbone_claim_ids`, `support_claim_ids`, and `callback_claim_ids`. The pipeline derives compatibility claim lists after normalization.
- Use `revision_mode` to decide edit depth: `local_edit` means adjust claim roles/placements; `act_rebuild` means rebuild the implicated act around its job and minimal backbone; `episode_rebuild` means change act count or macro sequence if needed.
- If an issue includes `minimal_backbone_claim_ids`, preserve or create that minimal backbone unless it conflicts with `episode_goal` / `root_question`; then drop or demote redundant claims around it.
- Treat `drop_or_demote_claim_ids`, `add_or_promote_claim_ids`, and `support_or_callback_claim_ids` as concrete evidence for the diagnosis, not a checklist. Prefer those claims when they solve the act job, but reshape the act structure if claim swaps alone cannot solve the problem.
- For the implicated act, make the smallest change that makes `title`, `act_job`, `opening_question`, and `ending_turn` true of the selected backbone. If the current backbone is stronger than the wording, rewrite the wording instead of swapping claims.
- Before applying a requested claim swap, identify the listener-work gap it fixes. If the issue is only claim preference, inventory expansion, or stronger-but-nonessential detail, preserve the plan and make only minimal role/timing edits.
- For a why/allowed/persisted issue, a requested causal/choice/rationale claim that fixes the gap belongs in the implicated act's backbone, not as later callback, unless you rewrite that act's question/job so it no longer asks that question.
- Do not swing in the next revision by demoting a causal/rationale claim merely because it answers well. It is premature only if it answers a later act's question and is not needed by the current `opening_question`/`act_job`.
- If a setup/hook act carries answer-revealing authority, cause, outcome, or mechanism claims in backbone, demote them to support/callback unless the act explicitly teaches that answer; keep the backbone on visible anomaly/stakes.
- Do not add a stress-test/case-study by default. Add or promote one only when the claims contain a strongly relevant proof-under-pressure beat, the plan lacks an equivalent test, and it improves the episode goal more than selected support/comparator/context.
- Preserve acts, timings, and claim placements that are not implicated by the issues. Edit surgically.
- If setup/hook backbone prematurely answers the episode question with answer-revealing authority, cause, outcome, or mechanism claims, demote those claims to support/callback or move them into the later explain/resolve act that owns the reveal.
- If setup/hook is overfunded for anomaly/recognition work, shorten it and give time to the hardest mechanism, crisis/test, decision, or payoff.
- Do not convert sequencing issues into a fixed story template. Choose the order that best serves the act's own `act_job`, then make `opening_question`, `internal_arc`, `ending_turn`, and claim roles match that choice.
- Revise the backbone first, then adjust act titles/purpose/opening/ending so the wording matches the revised claim set.
- Preserve the distinction between `backbone_claim_ids`, `support_claim_ids`, and `callback_claim_ids`. Do not hide unresolved overload by moving a claim from backbone to support if it would still require its own scene.
- If `scope` is `episode` or `revision_mode` is `episode_rebuild`, split or reshape acts before doing local claim edits.
- If `scope` is `act` or `revision_mode` is `act_rebuild`, rebuild the implicated act around its act job and minimal backbone rather than merely rewording the summary.
- Keep each claim in exactly one act.
- Keep all human-readable fields in `output_language`.
- Do not add claims that are not in the provided `claims` list.
- If you must change act count, do so only because the issues cannot be solved cleanly within the current count."""


def _showrunner_generation_metadata() -> dict[str, str]:
    provider = str(settings.showrunner_generation_provider or "anthropic").strip().lower()
    if provider in {"anthropic", "claude", "sonnet"}:
        return {"provider": "anthropic", "model": "claude-sonnet-4-6", "reasoning_effort": ""}
    if provider in {"openai", "gpt"}:
        reasoning_effort = str(settings.showrunner_openai_reasoning_effort or "").strip().lower()
        if reasoning_effort not in SHOWRUNNER_OPENAI_REASONING_EFFORTS:
            reasoning_effort = "medium"
        return {
            "provider": "openai",
            "model": str(settings.showrunner_openai_model or "gpt-5.4").strip() or "gpt-5.4",
            "reasoning_effort": reasoning_effort,
        }
    raise ValueError(
        "Unsupported SHOWRUNNER_GENERATION_PROVIDER "
        f"{settings.showrunner_generation_provider!r}; use 'anthropic' or 'openai'."
    )


async def _call_showrunner_generation_structured(
    *,
    system: str,
    prompt: str,
    temperature: float,
) -> str:
    metadata = _showrunner_generation_metadata()
    if metadata["provider"] == "openai":
        return await call_openai_structured(
            system=system,
            prompt=prompt,
            model=metadata["model"],
            schema=SHOWRUNNER_PLAN_SCHEMA,
            schema_name="showrunner_plan",
            temperature=None,
            reasoning_effort=metadata["reasoning_effort"] or None,
        )
    return await call_sonnet_structured(
        system=system,
        prompt=prompt,
        temperature=temperature,
    )


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
        generation_model: dict[str, str] | None = None
        try:
            quality_failures = 0
            attempt_recorded = False
            while True:
                attempt_recorded = False
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
                generation_model = _showrunner_generation_metadata()
                raw = await _call_showrunner_generation_structured(
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
                        "generation_model": generation_model,
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
                attempt_recorded = True
                if not blocker_issues:
                    plan = candidate_plan
                    selection_audit = candidate_selection_audit
                    break
                if quality_failures >= SHOWRUNNER_SELECTION_RETRIES:
                    plan = candidate_plan
                    selection_audit = candidate_selection_audit
                    attempt_records[-1]["returned"] = True
                    attempt_records[-1]["return_reason"] = "selection_audit_blockers_after_retries"
                    logger.warning(
                        "Showrunner selection audit still has %d medium/high blockers after %d retries; "
                        "returning latest candidate plan with audit metadata",
                        len(blocker_issues),
                        SHOWRUNNER_SELECTION_RETRIES,
                    )
                    break
                quality_failures += 1
                current_revision_plan = candidate_plan
                revision_request = {
                    "message": (
                        "Previous plan had editorial selection/design problems. "
                        "Revise the current plan instead of regenerating from scratch. "
                        "Fix the listed listener-work invariants directly. "
                        "If a why/allowed/persisted act lacks a causal-choice spine, "
                        "promote that claim to backbone or rewrite the act question/job."
                    ),
                    "selection_issues": blocker_issues,
                }
        except Exception:
            if not attempt_recorded:
                attempt_records.append(
                    {
                        "attempt_number": len(attempt_records) + 1,
                        "mode": "revision" if revision_request is not None else "initial",
                        "generation_model": generation_model,
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

        _align_conflict_quotas(
            plan,
            disputed_ids=disputed_ids,
            conflicts_summary=conflicts_summary,
        )

        # Update manifest
        if manifest:
            manifest.act_count = plan.get("act_count", len(plan.get("acts", [])))
            manifest.acts_json = plan.get("acts", [])
            await db.commit()

    _dump_showrunner_debug(
        run_id=run_id,
        status=(
            "ok_with_selection_audit_blockers"
            if _showrunner_selection_blockers(selection_audit)
            else "ok"
        ),
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


def _align_conflict_quotas(
    plan: dict,
    *,
    disputed_ids: list[str],
    conflicts_summary: list[dict],
) -> dict:
    disputed_set = set(disputed_ids)
    for act in plan.get("acts", []):
        if not isinstance(act, dict):
            continue
        claim_ids = set(_id_list(act.get("claim_ids")))
        quotas = act.get("content_quotas")
        if not isinstance(quotas, dict):
            quotas = {}
            act["content_quotas"] = quotas
        act_disputed = claim_ids & disputed_set
        act_conflicts = [
            conflict
            for conflict in conflicts_summary
            if claim_ids & set(_id_list(conflict.get("claim_ids")))
        ]
        if not act_disputed and not act_conflicts:
            quotas["conflicts"] = 0
            continue
        required_conflicts = max(1, len(act_disputed), len(act_conflicts))
        if _positive_int(quotas.get("conflicts"), 0) < required_conflicts:
            quotas["conflicts"] = required_conflicts
    return plan


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
        role_claim_ids = _union_preserve_order(
            *[_id_list(act.get(field)) for field in SHOWRUNNER_ACT_CLAIM_ROLE_FIELDS]
        )
        claim_ids = role_claim_ids or _id_list(act.get("claim_ids"))
        for claim_id in claim_ids:
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
        backbone_claim_ids = _id_list(act.get("backbone_claim_ids"))
        support_claim_ids = [
            claim_id for claim_id in _id_list(act.get("support_claim_ids")) if claim_id not in set(backbone_claim_ids)
        ]
        support_set = set(support_claim_ids)
        callback_claim_ids = [
            claim_id
            for claim_id in _id_list(act.get("callback_claim_ids"))
            if claim_id not in set(backbone_claim_ids) and claim_id not in support_set
        ]
        if not backbone_claim_ids and not support_claim_ids and not callback_claim_ids:
            backbone_claim_ids = _id_list(act.get("claim_ids"))
        role_pairs = (
            [(claim_id, "backbone") for claim_id in backbone_claim_ids]
            + [(claim_id, "support") for claim_id in support_claim_ids]
            + [(claim_id, "callback") for claim_id in callback_claim_ids]
        )
        acts_view.append(
            {
                "act_number": _positive_int(act.get("act_number"), 0),
                "story_role": str(act.get("story_role") or ""),
                "title": str(act.get("title") or ""),
                "purpose": str(act.get("purpose") or ""),
                "act_job": str(act.get("act_job") or ""),
                "internal_arc": str(act.get("internal_arc") or ""),
                "opening_question": str(act.get("opening_question") or ""),
                "ending_turn": str(act.get("ending_turn") or ""),
                "estimated_duration_seconds": _positive_int(act.get("estimated_duration_seconds"), 0),
                "backbone_claim_ids": backbone_claim_ids,
                "support_claim_ids": support_claim_ids,
                "callback_claim_ids": callback_claim_ids,
                "why_this_is_enough": str(act.get("why_this_is_enough") or ""),
                "claims": [
                    {**_compact_selection_claim_view(claim_lookup[claim_id]), "role": role}
                    for claim_id, role in role_pairs
                    if claim_id in claim_lookup
                ],
            }
        )
    return acts_view


def _normalize_showrunner_selection_issues(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed_severities = {"low", "medium", "high"}
    allowed_scopes = {"episode", "act", "claim_role", "claim"}
    allowed_revision_modes = {"local_edit", "act_rebuild", "episode_rebuild"}
    normalized: list[dict[str, Any]] = []
    for raw_issue in value:
        if not isinstance(raw_issue, dict):
            continue
        severity = str(raw_issue.get("severity") or "").strip().lower()
        scope = str(raw_issue.get("scope") or "").strip().lower()
        revision_mode = str(raw_issue.get("revision_mode") or "").strip().lower()
        if severity not in allowed_severities or scope not in allowed_scopes or revision_mode not in allowed_revision_modes:
            continue
        issue: dict[str, Any] = {
            "severity": severity,
            "scope": scope,
            "revision_mode": revision_mode,
            "diagnosis": str(raw_issue.get("diagnosis") or raw_issue.get("why_it_hurts") or "").strip(),
            "fix_direction": str(raw_issue.get("fix_direction") or "").strip(),
        }
        act_number = raw_issue.get("act_number")
        if isinstance(act_number, int) and act_number > 0:
            issue["act_number"] = act_number
        for field in (
            "affected_claim_ids",
            "minimal_backbone_claim_ids",
            "drop_or_demote_claim_ids",
            "add_or_promote_claim_ids",
            "support_or_callback_claim_ids",
        ):
            claim_ids = [item for item in raw_issue.get(field, []) if isinstance(item, str)]
            if claim_ids:
                issue[field] = claim_ids
        normalized.append(issue)
    return normalized


def _limit_showrunner_selection_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(issues) <= SHOWRUNNER_SELECTION_AUDIT_MAX_ISSUES:
        return issues
    high = [issue for issue in issues if issue.get("severity") == "high"]
    medium = [issue for issue in issues if issue.get("severity") == "medium"]
    low = [issue for issue in issues if issue.get("severity") == "low"]
    return (high + medium + low)[:SHOWRUNNER_SELECTION_AUDIT_MAX_ISSUES]


def _selection_act_role_index(plan: dict) -> dict[int, dict[str, set[str]]]:
    index: dict[int, dict[str, set[str]]] = {}
    for act in plan.get("acts", []):
        if not isinstance(act, dict):
            continue
        act_number = _positive_int(act.get("act_number"), 0)
        if act_number <= 0:
            continue
        backbone = set(_id_list(act.get("backbone_claim_ids")))
        support = {claim_id for claim_id in _id_list(act.get("support_claim_ids")) if claim_id not in backbone}
        callback = {
            claim_id
            for claim_id in _id_list(act.get("callback_claim_ids"))
            if claim_id not in backbone and claim_id not in support
        }
        if not backbone and not support and not callback:
            backbone = set(_id_list(act.get("claim_ids")))
        index[act_number] = {
            "backbone": backbone,
            "support": support,
            "callback": callback,
            "selected": backbone | support | callback,
        }
    return index


def _validate_showrunner_selection_issue_ids(
    issues: list[dict[str, Any]],
    *,
    selected_ids: set[str],
    omitted_ids: set[str],
    all_ids: set[str] | None = None,
    act_roles: dict[int, dict[str, set[str]]] | None = None,
) -> list[dict[str, Any]]:
    known_ids = all_ids or selected_ids | omitted_ids
    validated: list[dict[str, Any]] = []
    for issue in issues:
        act_number = issue.get("act_number")
        target_roles = act_roles.get(act_number) if isinstance(act_number, int) and act_roles else None
        target_selected_ids = target_roles["selected"] if target_roles else selected_ids
        target_backbone_ids = target_roles["backbone"] if target_roles else set()
        target_support_ids = target_roles["support"] if target_roles else set()
        target_callback_ids = target_roles["callback"] if target_roles else set()

        affected_claim_ids = [
            claim_id
            for claim_id in issue.get("affected_claim_ids", [])
            if isinstance(claim_id, str)
            and claim_id in known_ids
            and (not target_roles or claim_id in target_selected_ids or claim_id in omitted_ids)
        ]
        minimal_backbone_claim_ids = [
            claim_id
            for claim_id in issue.get("minimal_backbone_claim_ids", [])
            if isinstance(claim_id, str) and claim_id in known_ids
        ]
        drop_or_demote_claim_ids = [
            claim_id
            for claim_id in issue.get("drop_or_demote_claim_ids", [])
            if isinstance(claim_id, str)
            and (
                (claim_id in target_backbone_ids or claim_id in target_support_ids)
                if target_roles
                else claim_id in selected_ids
            )
        ]
        add_or_promote_claim_ids = [
            claim_id
            for claim_id in issue.get("add_or_promote_claim_ids", [])
            if isinstance(claim_id, str)
            and claim_id in known_ids
            and (
                (claim_id in omitted_ids or claim_id in target_support_ids or claim_id in target_callback_ids)
                if target_roles
                else True
            )
        ]
        support_or_callback_claim_ids = [
            claim_id
            for claim_id in issue.get("support_or_callback_claim_ids", [])
            if isinstance(claim_id, str)
            and ((claim_id in target_backbone_ids) if target_roles else claim_id in selected_ids)
        ]

        required_ids = set(minimal_backbone_claim_ids)
        demoted_ids = set(drop_or_demote_claim_ids) | set(support_or_callback_claim_ids)
        promoted_ids = set(add_or_promote_claim_ids)
        if required_ids & demoted_ids:
            continue
        if promoted_ids & demoted_ids:
            continue

        claim_action_references = any(
            (
                affected_claim_ids,
                drop_or_demote_claim_ids,
                add_or_promote_claim_ids,
                support_or_callback_claim_ids,
            )
        )
        has_valid_claim_reference = any(
            (
                affected_claim_ids,
                minimal_backbone_claim_ids,
                drop_or_demote_claim_ids,
                add_or_promote_claim_ids,
                support_or_callback_claim_ids,
            )
        )
        if issue.get("scope") in {"claim", "claim_role"} and not claim_action_references:
            continue
        if not has_valid_claim_reference and issue.get("scope") not in {"episode", "act"}:
            continue
        cleaned = dict(issue)
        cleaned["affected_claim_ids"] = affected_claim_ids
        cleaned["minimal_backbone_claim_ids"] = minimal_backbone_claim_ids
        cleaned["drop_or_demote_claim_ids"] = drop_or_demote_claim_ids
        cleaned["add_or_promote_claim_ids"] = add_or_promote_claim_ids
        cleaned["support_or_callback_claim_ids"] = support_or_callback_claim_ids
        for field in (
            "affected_claim_ids",
            "minimal_backbone_claim_ids",
            "drop_or_demote_claim_ids",
            "add_or_promote_claim_ids",
            "support_or_callback_claim_ids",
        ):
            if not cleaned[field]:
                cleaned.pop(field, None)
        validated.append(cleaned)
    return validated


def _selection_audit_raw_signals_failure(raw: str) -> bool:
    compact = "".join(str(raw or "").lower().split())
    return (
        '"passed":false' in compact
        or '"severity":"medium"' in compact
        or '"severity":"high"' in compact
    )


def _selection_audit_parse_failure_response(raw: str, exc: Exception) -> dict[str, Any]:
    if _selection_audit_raw_signals_failure(raw):
        return {
            "passed": False,
            "rationale": (
                "Selection audit returned invalid JSON while signaling a failure; treating it as blocking "
                "instead of accepting an unaudited plan."
            ),
            "issues": [
                {
                    "severity": "high",
                    "scope": "episode",
                    "revision_mode": "episode_rebuild",
                    "diagnosis": "Selection audit output was invalid or truncated while indicating the plan failed.",
                    "fix_direction": (
                        "Revise the candidate and rerun the audit with compact JSON; do not accept this plan "
                        "as auditor-approved."
                    ),
                }
            ],
            "audit_failed": True,
            "raw_signaled_failure": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "raw": raw,
            "parsed": None,
        }
    return {
        "passed": True,
        "rationale": "",
        "issues": [],
        "audit_failed": True,
        "raw_signaled_failure": False,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "raw": raw,
        "parsed": None,
    }


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
            "duration_target_seconds": prompt_payload.get("duration_target_seconds", 0),
            "acts": _compact_selection_act_view(audit_plan, claim_lookup),
            "omitted_claims": omitted_claims,
            "task": (
                "Audit act-contract failures in the selected backbone only. "
                "Return medium/high issues only where the act spine cannot pay its title/job/opening/ending promise. "
                "Treat optional alternatives as low severity or mention them in rationale. Return compact JSON only."
            ),
        },
        ensure_ascii=False,
    )
    raw = ""
    try:
        raw = await call_haiku_structured(
            system=SHOWRUNNER_SELECTION_AUDIT_SYSTEM,
            prompt=audit_prompt,
            max_tokens=SHOWRUNNER_SELECTION_AUDIT_MAX_TOKENS,
        )
        parsed = parse_llm_json(raw)
    except Exception as exc:
        failure_response = _selection_audit_parse_failure_response(raw, exc)
        logger.warning(
            "Showrunner selection audit failed after a valid candidate plan; raw_signaled_failure=%s: %s",
            failure_response["raw_signaled_failure"],
            exc,
        )
        return failure_response
    issues = _limit_showrunner_selection_issues(
        _validate_showrunner_selection_issue_ids(
            _normalize_showrunner_selection_issues(parsed.get("issues") if isinstance(parsed, dict) else None),
            selected_ids=selected_ids,
            omitted_ids=omitted_ids,
            all_ids=set(claim_lookup),
            act_roles=_selection_act_role_index(audit_plan),
        )
    )
    rationale = str(parsed.get("rationale") or "").strip() if isinstance(parsed, dict) else ""
    passed = (bool(parsed.get("passed", not issues)) if isinstance(parsed, dict) else not issues) or not issues
    return {
        "passed": passed and not _showrunner_selection_blockers({"issues": issues}),
        "rationale": rationale,
        "issues": issues,
        "raw": raw,
        "parsed": parsed if isinstance(parsed, dict) else None,
    }


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
    acts_raw = plan.get("acts")
    if not isinstance(acts_raw, list):
        return {key: value for key, value in plan.items() if key != "claim_ids"}
    shortened_plan = dict(plan)
    shortened_acts: list[dict[str, Any]] = []
    for act in acts_raw:
        if not isinstance(act, dict):
            continue
        backbone_claim_ids = _uuid_list(act.get("backbone_claim_ids"))
        support_claim_ids = _uuid_list(act.get("support_claim_ids"))
        callback_claim_ids = _uuid_list(act.get("callback_claim_ids"))
        if not backbone_claim_ids and not support_claim_ids and not callback_claim_ids:
            backbone_claim_ids = _uuid_list(act.get("claim_ids"))
        shortened_acts.append(
            {
                **{key: value for key, value in act.items() if key != "claim_ids"},
                "backbone_claim_ids": sid.shorten_list(backbone_claim_ids) if sid else backbone_claim_ids,
                "support_claim_ids": sid.shorten_list(support_claim_ids) if sid else support_claim_ids,
                "callback_claim_ids": sid.shorten_list(callback_claim_ids) if sid else callback_claim_ids,
            }
        )
    shortened_plan["acts"] = shortened_acts
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


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _union_preserve_order(*groups: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
    return out


def _restore_plan_claim_ids(value: Any, *, sid: ShortIdMap | None) -> list[str]:
    return restore_id_list(value, sid) if sid else _uuid_list(value)


def _normalize_act_claim_roles(act: dict[str, Any], *, sid: ShortIdMap | None) -> dict[str, list[str]]:
    explicit_claim_ids = _restore_plan_claim_ids(act.get("claim_ids"), sid=sid)
    roles_provided = any(isinstance(act.get(field), list) for field in SHOWRUNNER_ACT_CLAIM_ROLE_FIELDS)
    backbone_claim_ids = _restore_plan_claim_ids(act.get("backbone_claim_ids"), sid=sid)
    support_claim_ids = _restore_plan_claim_ids(act.get("support_claim_ids"), sid=sid)
    callback_claim_ids = _restore_plan_claim_ids(act.get("callback_claim_ids"), sid=sid)

    if not roles_provided:
        claim_ids = _dedupe_preserve_order(explicit_claim_ids)
        return {
            "claim_ids": claim_ids,
            "backbone_claim_ids": list(claim_ids),
            "support_claim_ids": [],
            "callback_claim_ids": [],
        }

    role_union = _union_preserve_order(backbone_claim_ids, support_claim_ids, callback_claim_ids)
    unassigned_claim_ids = [claim_id for claim_id in explicit_claim_ids if claim_id not in set(role_union)]
    if not backbone_claim_ids and unassigned_claim_ids:
        backbone_claim_ids = list(unassigned_claim_ids)
        unassigned_claim_ids = []

    backbone_claim_ids = _dedupe_preserve_order(backbone_claim_ids)
    backbone_set = set(backbone_claim_ids)
    support_claim_ids = _dedupe_preserve_order(
        [claim_id for claim_id in support_claim_ids + unassigned_claim_ids if claim_id not in backbone_set]
    )
    support_set = set(support_claim_ids)
    callback_claim_ids = _dedupe_preserve_order(
        [claim_id for claim_id in callback_claim_ids if claim_id not in backbone_set and claim_id not in support_set]
    )
    claim_ids = _union_preserve_order(backbone_claim_ids, support_claim_ids, callback_claim_ids)
    if claim_ids and not backbone_claim_ids:
        raise RuntimeError("Showrunner act selected support/callback claims but no backbone claims")
    return {
        "claim_ids": claim_ids,
        "backbone_claim_ids": backbone_claim_ids,
        "support_claim_ids": support_claim_ids,
        "callback_claim_ids": callback_claim_ids,
    }


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

        act_job = act.get("act_job")
        if not isinstance(act_job, str):
            act_job = ""

        internal_arc = act.get("internal_arc")
        if not isinstance(internal_arc, str):
            internal_arc = ""

        opening_question = act.get("opening_question")
        if not isinstance(opening_question, str):
            opening_question = ""

        ending_turn = act.get("ending_turn")
        if not isinstance(ending_turn, str):
            ending_turn = ""

        why_this_is_enough = act.get("why_this_is_enough")
        if not isinstance(why_this_is_enough, str):
            why_this_is_enough = ""

        claim_roles = _normalize_act_claim_roles(act, sid=sid)

        normalized_acts.append(
            {
                "act_number": act_number,
                "story_role": story_role.strip(),
                "title": title.strip(),
                "purpose": purpose.strip(),
                "act_job": act_job.strip(),
                "internal_arc": internal_arc.strip(),
                "opening_question": opening_question.strip(),
                "ending_turn": ending_turn.strip(),
                "estimated_duration_seconds": _positive_int(
                    act.get("estimated_duration_seconds"),
                    default_act_duration,
                ),
                "claim_ids": claim_roles["claim_ids"],
                "backbone_claim_ids": claim_roles["backbone_claim_ids"],
                "support_claim_ids": claim_roles["support_claim_ids"],
                "callback_claim_ids": claim_roles["callback_claim_ids"],
                "conflict_brief_ids": _uuid_list(act.get("conflict_brief_ids")),
                "content_quotas": content_quotas,
                "why_this_is_enough": why_this_is_enough.strip(),
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
        roles_provided = any(isinstance(act.get(field), list) for field in SHOWRUNNER_ACT_CLAIM_ROLE_FIELDS)
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
        allowed_claim_ids = set(deduped_claim_ids)
        for field in SHOWRUNNER_ACT_CLAIM_ROLE_FIELDS:
            act[field] = [claim_id for claim_id in _uuid_list(act.get(field)) if claim_id in allowed_claim_ids]
        if deduped_claim_ids and not act.get("backbone_claim_ids"):
            if roles_provided:
                raise RuntimeError(
                    f"Showrunner act {_positive_int(act.get('act_number'), idx + 1)} has selected claims but no backbone claims"
                )
            act["backbone_claim_ids"] = list(deduped_claim_ids)

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
