"""Phase 6.2: Dialogue audit + conditional polish."""

from __future__ import annotations

from collections import Counter
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.models.show_bible import ShowBible
from app.models.script_segment import AuditStatus, ScriptSegment
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.llm_json import parse_llm_json
from app.pipeline.short_ids import ShortIdMap, restore_dialogue_claim_ids, shorten_dialogue_claim_ids
from app.pipeline.phase6_writing.dialogue_utils import (
    build_speaker_aliases as _build_speaker_aliases,
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
ACT_FINDING_FAMILIES = {
    "replay_question",
    "replay_punchline",
    "repeated_explanatory_reset",
    "repeated_confirmation_first_opening",
    "repeated_interruption_first_opening",
    "speaker_distribution_imbalance",
    "other",
}
SEAM_STATUSES = {
    "acceptable_handoff",
    "thesis_to_example_progression",
    "replay_question",
    "replay_punchline",
    "weak_reset",
}
PROBLEMATIC_SEAM_STATUSES = {"replay_question", "replay_punchline", "weak_reset"}
REPETITION_PRIORITY_TYPES = {
    "scene_opening_echo",
    "boundary_restatement",
    "repetitive_agreement_shape",
    "repetitive_challenge_shape",
    "rigid_turn_taking",
    "teacherly_exposition",
}
COMPREHENSION_ISSUE_TYPES = {
    "undefined_actor",
    "unexplained_term",
    "namedrop_without_grounding",
    "causal_gap",
    "missing_bridge",
    "assumed_context",
    "summary_instead_of_scene",
    "compressed_history_dump",
    "payoff_restarts_explanation",
}
ACT_MONOTONY_ROUTING_FAMILIES = {
    "repeated_explanatory_reset",
    "repeated_confirmation_first_opening",
    "repeated_interruption_first_opening",
}
MULTI_SCENE_POLISH_FAMILIES = {"repeated_explanatory_reset"}
MIN_MULTI_SCENE_CLUSTER_SIZE = 3
MAX_MULTI_SCENE_CLUSTER_SIZE = 4
MAX_AUDIT_ISSUES = 3
MAX_AUDIT_TEXT_CHARS = 220

EVAL_SYSTEM = """You are a podcast dialogue quality auditor.
Score the scene for engagement quality (0.0 to 1.0) and return a compact machine-facing report.

Return JSON:
{
  "score": 0.75,
  "passed": true,
  "issues": [
    {
      "type": "dry_facts|persona_mismatch|repetitive|no_examples|rigid_turn_taking|repetitive_agreement_shape|repetitive_challenge_shape|scene_opening_echo|teacherly_exposition|boundary_restatement|undefined_actor|unexplained_term|namedrop_without_grounding|causal_gap|missing_bridge|assumed_context|summary_instead_of_scene|compressed_history_dump|payoff_restarts_explanation",
      "severity": "low|medium|high",
      "scope": "scene_polish|scene_rewrite|act_warning",
      "description": "short machine-facing diagnosis in English",
      "suggestion": "short machine-facing repair hint in English"
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
- scene-to-scene echo only when the new scene opens by closely repeating or paraphrasing the previous scene's punchline without adding a new angle, consequence, example, or pressure
- boundary restatement only when the opening merely restates what the boundary context already established instead of advancing the scene's move
- undefined actors: the dialogue leaves it unclear who acted, decided, issued, changed policy, or intervened
- unexplained terms: a technical, legal, or institutional label appears without enough local explanation for a non-expert listener
- namedrops without grounding: a person, institution, law, or date appears as if already meaningful
- causal gaps: the scene jumps from setup to consequence without a clear bridge
- assumed context: the dialogue depends on background knowledge that the scene should not assume
- summary instead of scene: the exchange mostly states conclusions rather than making the beat legible step by step
- compressed history dump: chronology or institutional change is packed too tightly to follow comfortably
- payoff restarts explanation: a late payoff / closing beat reopens already-covered mechanism instead of advancing the current question
- rigid turn-taking that makes the exchange feel mechanically alternating
- repetitive agreement shapes where explicit confirmations dominate the rhythm
- repetitive challenge shapes where objections or questions sound too similar
- teacherly exposition where dialogue explains correctly but without enough conversational movement
- use `scene_metadata` when present as the intended clarity contract for the scene

Compactness contract:
- return at most 3 issues
- choose the dominant root causes, not every visible symptom
- do not report the same root cause twice under different labels
- keep each `description` and `suggestion` brief and operational
- if the scene is locally acceptable but mainly points to an act-level rhythm problem, prefer `act_warning`

Do not overflag normal continuity:
- topic bridge or thesis-to-example progression is not echo by itself
- zooming from a general point to a concrete case is acceptable if the new scene genuinely advances
- a narrower next question is acceptable if it moves the conversation forward instead of re-asking the same question

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
- Write `description` and `suggestion` in English for consistency across internal audits.
- Reason about natural spoken register for the payload language rather than assuming English defaults.
- Output only JSON."""

POLISH_SYSTEM = """You are polishing a podcast dialogue scene for naturalness and flow.

Return JSON:
{
  "dialogue": [
    {"speaker": "A", "text": "line", "claim_ids": ["id"], "emotion": "measured", "ssml_hints": {"pause_ms": 180}},
    {"speaker": "B", "text": "line", "claim_ids": [], "emotion": "skeptical", "ssml_hints": {"pause_ms": 220}}
  ]
}

Rules:
- Treat this as a targeted intervention, not a generic rewrite.
- The payload includes `language`; keep the scene natural for that language and spoken register.
- The payload may include `scene_metadata`; use it as the clarity contract for what the listener should understand by the end.
- Keep spoken lines in the payload language. If the original scene contains a foreign-language quote or source snippet and the beat is not explicitly about the wording itself, rewrite it into the payload language instead of preserving the raw source-language text.
- Keep the `speaker` field canonical: output only `A` or `B`, never host names, role names, or localized speaker variants.
- `claim_ids` are opaque labels. Reuse only labels already present in the input dialogue and copy them exactly.
- Preserve the factual meaning, chronology, required claim coverage, and scene order.
- Preserve attribution and institutional details already present.
- Preserve line-level delivery intent. If a line already carries `emotion` or `ssml_hints`, return compatible delivery metadata instead of dropping it.
- If you split, merge, or otherwise change line alignment, every output line must still carry compatible delivery metadata so downstream TTS cues remain usable.
- Do not add new facts, names, dates, figures, laws, or causal claims.
- Keep the scene's `scene_job` intact. Improve delivery without changing the scene's intended move.
- Use `intervention_targets`, `intervention_mode`, and `success_criteria` as the primary contract. Generic style cleanup is secondary.
- The payload may include an `act_repetition_report`; if one opener, agreement, or turn-taking pattern is dominating the act, use this scene to break that pattern naturally.
- The payload may include a `seam_finding`; if this scene is opening a new act by replaying the previous act's question or punchline, fix that seam without flattening the scene.
- If the target problem is comprehension, prioritize listener clarity over clever phrasing: make actors, causality, and any new term legible without adding new facts.
- The payload may include `intervention_targets`; solve those concrete dialogue problems, not generic "rewrite for style" goals.
- `intervention_mode` tells you which repair type matters most: opener repair, seam repair, rhythm repair, agreement-shape repair, clarity repair, or general polish.
- `success_criteria` lists what must become true for this intervention to count as solved.
- If the boundary context already landed a punchline, do not reopen by paraphrasing it.
- If this scene opens a new act, continue the same conversation instead of resetting into a more basic pedagogical question unless the scene's job explicitly requires that reset.
- Do not default to `B asks -> A confirms -> A explains`. B may contribute with a hypothesis, a concrete observation, an objection, a consequence, or a partial answer.
- When fixing repetitive agreement shapes, prefer removing explicit confirmation markers and changing the move, not just swapping one confirmation phrase for another.
- For opener or seam repair, rewrite the first 1-2 turns decisively. Do not keep the same reset and merely soften the wording.
- For rhythm repair, change the conversational move, not just the phrasing. Replace restatement with implication, objection, hypothesis, consequence, or example when possible.
- Rewrite the whole scene if needed for rhythm, but keep the same scene job and content function.
- Avoid translated-from-English phrasing or register drift.
- Keep only speakers `A` and `B`.
- Output only JSON."""

MULTI_SCENE_POLISH_SYSTEM = """You are polishing a contiguous cluster of podcast dialogue scenes inside one act.

Return JSON:
{
  "scenes": [
    {
      "scene_number": 3,
      "dialogue": [
        {"speaker": "A", "text": "line", "claim_ids": ["id"], "emotion": "measured", "ssml_hints": {"pause_ms": 180}},
        {"speaker": "B", "text": "line", "claim_ids": [], "emotion": "skeptical", "ssml_hints": {"pause_ms": 220}}
      ]
    }
  ]
}

Rules:
- Treat this as a targeted cluster intervention, not an act rewrite.
- The payload includes `language`; keep all scenes natural for that language and spoken register.
- Keep the `speaker` field canonical: output only `A` or `B`.
- Preserve the scene order, scene jobs, factual meaning, chronology, attribution, and required claim coverage.
- `claim_ids` are opaque labels. Reuse only labels already present in the corresponding input scene and copy them exactly.
- Do not move claim material from one scene into another just to create fake variety.
- Do not add new facts, names, dates, figures, laws, or causal claims.
- Preserve delivery metadata compatibility for every output line.
- The goal is to reduce cluster-level monotony, not to make every scene louder or more dramatic.
- Use `cluster_issue`, `cluster_goal`, and `success_criteria` as the primary contract.
- Across the cluster, avoid repeating the same opener strategy in every scene.
- Do not let all scenes open with the same skeptical-question reset.
- Do not make every scene open as `B asks a practical/skeptical question -> A explains`.
- Let at least one scene advance through consequence, implication, concrete oddity, or institutional contrast instead of a fresh explanatory reset.
- Rewrite as much of each scene as needed to vary the conversational move, but keep each scene anchored in its declared job.
- Output one rewritten dialogue for every input `scene_number`.
- Output only JSON."""

ACT_AUDIT_SYSTEM = """You are an act-level podcast dialogue auditor.
Review one full act as a sequence of scenes and identify aggregate repetition / distribution problems.

Return JSON:
{
  "priority_scene_numbers": [2, 4],
  "distribution_findings": [
    {
      "family": "replay_question|replay_punchline|repeated_explanatory_reset|repeated_confirmation_first_opening|repeated_interruption_first_opening|speaker_distribution_imbalance|other",
      "severity": "low|medium|high",
      "scene_numbers": [2, 4],
      "description": "what repeated in this act and why it weakens the act"
    }
  ]
}

Rules:
- The payload includes `language`; judge the act as spoken dialogue in that language.
- Focus on act-level distribution problems, not local factual rigor.
- The payload includes the full scene dialogue for the act, plus a compact structure summary.
- Distinguish acceptable continuity from bad replay.
- Thesis-to-example progression or zoom-in from concept to case is often acceptable and should not be flagged as replay by default.
- Flag `replay_question` only when a new scene re-asks the same question already established by a previous scene.
- Flag `replay_punchline` only when a new scene re-states the previous scene's conclusion instead of advancing to consequence, example, or pressure.
- Use the full act text to identify repeated opener strategies, repeated explanatory resets, repeated confirmation/challenge habits, and speaker-distribution imbalance across scenes.
- Prioritize the smallest set of scenes whose polishing would most improve the act's variety and flow.
- `priority_scene_numbers` should contain scenes that are the best intervention points, not every scene with a local issue.
- Output only JSON."""

SEAM_AUDIT_SYSTEM = """You are auditing episode seams between acts in a podcast script.

Return JSON:
{
  "seams": [
    {
      "from_act_number": 1,
      "to_act_number": 2,
      "target_scene_number": 1,
      "status": "acceptable_handoff|thesis_to_example_progression|replay_question|replay_punchline|weak_reset",
      "severity": "low|medium|high",
      "description": "what this seam is doing and why it works or fails"
    }
  ]
}

Rules:
- The payload includes `language`; judge the seams as spoken dialogue in that language.
- You are not grading the whole act. Only judge the seam between the previous act's final scene and the next act's opening scene.
- `acceptable_handoff` means the new act continues naturally with a new angle, consequence, example, or pressure.
- `thesis_to_example_progression` means the new act opens by making the previous act's thesis concrete; this is acceptable and should not trigger repair by itself.
- `replay_question` means the new act re-asks the same question that the previous act already landed.
- `replay_punchline` means the new act re-states the previous act's conclusion instead of moving forward.
- `weak_reset` means the new act opening is not a direct replay, but still resets weakly and misses a stronger new move.
- `target_scene_number` should be the opening scene number of the next act when a seam problem exists.
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
    seam_reports: list[dict[str, Any]] = []

    async with async_session_factory() as db:
        speaker_aliases: dict[str, str] = {}
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()
            if bible is not None:
                speaker_aliases = _build_speaker_aliases(bible.hosts)
        segments = await _load_segments_for_audit(db, segment_ids)
        scene_metadata_lookup = await _load_manifest_scene_metadata_lookup(db, state.get("manifest_id"))
        scene_job_lookup = {
            key: str(metadata.get("scene_job") or "").strip()
            for key, metadata in scene_metadata_lookup.items()
            if isinstance(metadata, dict) and str(metadata.get("scene_job") or "").strip()
        }

        for idx, segment in enumerate(segments):
            dialogue_text = json.dumps(segment.dialogue_json, ensure_ascii=False)
            if len(dialogue_text) < 50:
                continue

            previous_segment = segments[idx - 1] if idx > 0 else None
            boundary_context = _build_boundary_context(previous_segment)
            scene_metadata = scene_metadata_lookup.get((segment.act_number, segment.scene_number), {})
            scene_job = scene_job_lookup.get((segment.act_number, segment.scene_number), "")
            prompt_payload = _build_dialogue_audit_prompt_payload(
                segment=segment,
                language=language,
                boundary_context=boundary_context,
                scene_job=scene_job,
                scene_metadata=scene_metadata,
            )
            prompt_payload = _shorten_dialogue_prompt_claim_ids(
                prompt_payload,
                _build_dialogue_claim_sid(segment.dialogue_json),
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
                    "scene_metadata": scene_metadata,
                    "can_intervene": segment.revision_count < settings.max_writer_revolt_per_scene,
                }
            )

        act_reports = _build_act_dialogue_reports(audit_rows)
        act_reports = await _augment_act_dialogue_reports_with_llm(
            audit_rows=audit_rows,
            act_reports=act_reports,
            language=language,
        )
        seam_reports = await _run_episode_seam_audit(
            segments=segments,
            scene_job_lookup=scene_job_lookup,
            language=language,
            run_id=run_id,
        )
        seam_report_by_target = _seam_reports_by_target(seam_reports)

        for row in audit_rows:
            segment = row["segment"]
            row["seam_finding"] = seam_report_by_target.get((segment.act_number, segment.scene_number))
            act_report = act_reports.get(_act_count_key(segment.act_number), {})
            row["intervention_targets"] = _dialogue_intervention_targets(
                row=row,
                act_report=act_report,
                seam_finding=row.get("seam_finding"),
            )
        _refresh_dialogue_priorities(audit_rows, act_reports)
        multi_scene_candidates = _build_multi_scene_polish_candidates(
            audit_rows=audit_rows,
            act_reports=act_reports,
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
                    "scene_metadata": row.get("scene_metadata"),
                    "polish_applied": False,
                    "rewrite_requested": False,
                    "priority_score": row.get("priority_score", 0),
                    "act_report": _dialogue_act_report_excerpt(act_report),
                    "seam_finding": row.get("seam_finding"),
                    "intervention_targets": row.get("intervention_targets") or [],
                    "multi_scene_candidate": row.get("multi_scene_candidate"),
                    "can_intervene": row["can_intervene"],
                },
            )

        cluster_polish_results: list[dict[str, Any]] = []
        for candidate in multi_scene_candidates:
            act_key = _act_count_key(candidate["act_number"])
            act_revolt_count = revolt_counts_by_act.get(act_key, 0)
            if act_revolt_count >= settings.max_writer_revolt_per_act:
                continue
            accepted = await _attempt_multi_scene_polish(
                db=db,
                run_id=run_id,
                candidate=candidate,
                language=language,
                speaker_aliases=speaker_aliases,
                act_repetition_report=_dialogue_act_report_excerpt(act_reports.get(act_key)),
            )
            if not accepted:
                continue
            polished_scenes += len(candidate["rows"])
            revolt_counts_by_act[act_key] = act_revolt_count + 1
            cluster_polish_results.append(
                {
                    "act_number": candidate["act_number"],
                    "family": candidate["family"],
                    "scene_numbers": candidate["scene_numbers"],
                }
            )
            for row in candidate["rows"]:
                row["cluster_polished"] = True

        for row in _prioritize_dialogue_interventions(audit_rows, act_reports):
            segment = row["segment"]
            issues = row["issues"]
            trigger_label = (
                str(issues[0].get("type") or "").strip()
                if issues
                else str((row.get("intervention_targets") or ["act_distribution"])[0] or "act_distribution")
            )
            trigger_label = trigger_label.replace(":", "_")
            if not row["can_intervene"]:
                continue
            if bool(row.get("cluster_polished")):
                continue

            act_key = _act_count_key(segment.act_number)
            act_revolt_count = revolt_counts_by_act.get(act_key, 0)
            if act_revolt_count >= settings.max_writer_revolt_per_act:
                continue

            act_report = act_reports.get(act_key, {})
            if _row_should_polish_dialogue(row):
                publish_event(
                    run_id,
                    writer_revolt(segment.act_number, segment.scene_number, trigger_label),
                )
                accepted = await _attempt_dialogue_polish(
                    db=db,
                    run_id=run_id,
                    segment=segment,
                    language=language,
                    speaker_aliases=speaker_aliases,
                    boundary_context=row["boundary_context"],
                    scene_job=row["scene_job"],
                    scene_metadata=row.get("scene_metadata"),
                    issues=issues,
                    act_repetition_report=_dialogue_act_report_excerpt(act_report),
                    seam_finding=row.get("seam_finding"),
                    intervention_targets=row.get("intervention_targets") or [],
                )
                if accepted:
                    polished_scenes += 1
                    revolt_counts_by_act[act_key] = act_revolt_count + 1
                    continue

            if _should_request_scene_rewrite(issues):
                publish_event(
                    run_id,
                    writer_revolt(segment.act_number, segment.scene_number, trigger_label),
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
    seam_reports = _finalize_dialogue_seam_reports(seam_reports=seam_reports, audit_rows=audit_rows)
    quality_signals["dialogue_flagged_scenes"] = flagged_scenes
    quality_signals["dialogue_polished_scenes"] = polished_scenes
    quality_signals["dialogue_rewrite_requested_scenes"] = rewrite_requested_scenes
    quality_signals["dialogue_revolt_counts_by_act"] = revolt_counts_by_act
    quality_signals["dialogue_act_reports"] = act_reports
    quality_signals["dialogue_act_monotony_signals"] = {
        act_key: _dialogue_act_monotony_signals(report)
        for act_key, report in act_reports.items()
    }
    quality_signals["dialogue_multi_scene_polish_clusters"] = cluster_polish_results
    quality_signals["dialogue_rhythm_metrics"] = _build_dialogue_rhythm_metrics(segments)
    quality_signals["dialogue_rhythm_metrics_by_act"] = _build_dialogue_rhythm_metrics_by_act(segments)
    quality_signals["dialogue_timing"] = _estimate_dialogue_timing(
        segments=segments,
        scene_metadata_lookup=scene_metadata_lookup,
    )
    quality_signals["dialogue_seam_reports"] = seam_reports
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
    scene_metadata: dict[str, Any] | None = None,
    issues: list[dict[str, Any]],
    act_repetition_report: dict[str, Any] | None,
    seam_finding: dict[str, Any] | None = None,
    intervention_targets: list[str] | None = None,
    speaker_aliases: dict[str, str] | None = None,
) -> bool:
    original_dialogue = segment.dialogue_json
    original_claim_ids = set(_extract_dialogue_claim_ids(original_dialogue))
    claim_sid = ShortIdMap(list(original_claim_ids), prefix="c")
    prompt_payload = _build_dialogue_polish_prompt_payload(
        segment=segment,
        language=language,
        scene_job=scene_job,
        scene_metadata=scene_metadata,
        boundary_context=boundary_context,
        issues=issues,
        act_repetition_report=act_repetition_report,
        seam_finding=seam_finding,
        intervention_targets=intervention_targets or [],
    )
    prompt_payload = _shorten_dialogue_prompt_claim_ids(prompt_payload, claim_sid)

    try:
        raw = await call_sonnet_structured(
            system=POLISH_SYSTEM,
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            max_tokens=2048,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        polished_dialogue = _normalize_dialogue_output(
            parsed.get("dialogue"),
            speaker_aliases=speaker_aliases,
        )
        polished_dialogue = restore_dialogue_claim_ids(polished_dialogue, claim_sid)
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

    final_dialogue = _finalize_polished_dialogue(
        original_dialogue=original_dialogue,
        polished_dialogue=polished_dialogue,
    )
    if not final_dialogue:
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="metadata_contract_failed",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=polished_dialogue,
            decision="rejected",
            error="polish did not preserve delivery metadata safely",
        )
        return False

    if not _dialogue_polish_resolves_targets(
        original_dialogue=original_dialogue,
        polished_dialogue=final_dialogue,
        issues=issues,
        boundary_context=boundary_context,
        act_repetition_report=act_repetition_report,
        seam_finding=seam_finding,
        intervention_targets=intervention_targets or [],
        scene_metadata=scene_metadata,
    ):
        _dump_dialogue_polish_debug(
            run_id=run_id,
            act_number=segment.act_number,
            scene_number=segment.scene_number,
            status="target_not_resolved",
            payload=prompt_payload,
            before_dialogue=original_dialogue,
            after_dialogue=final_dialogue,
            decision="rejected",
            error="polish changed the scene but did not resolve the targeted opener/repetition problem",
        )
        return False

    post_polish_seam_finding: dict[str, Any] | None = None
    if _should_recheck_seam_after_polish(
        segment=segment,
        seam_finding=seam_finding,
        intervention_targets=intervention_targets or [],
    ):
        post_polish_seam_finding = await _rerun_polished_scene_seam_audit(
            run_id=run_id,
            segment=segment,
            language=language,
            scene_job=scene_job,
            boundary_context=boundary_context,
            seam_finding=seam_finding,
            polished_dialogue=final_dialogue,
        )
        if isinstance(post_polish_seam_finding, dict) and str(post_polish_seam_finding.get("status") or "").strip() in PROBLEMATIC_SEAM_STATUSES:
            _dump_dialogue_polish_debug(
                run_id=run_id,
                act_number=segment.act_number,
                scene_number=segment.scene_number,
                status="target_not_resolved",
                payload=prompt_payload,
                before_dialogue=original_dialogue,
                after_dialogue=final_dialogue,
                decision="rejected",
                error="polish improved the scene locally but did not resolve the targeted seam problem",
            )
            return False

    segment.dialogue_json = final_dialogue
    segment.revision_count += 1
    _set_segment_audit_finding(
        segment,
        "dialogue",
        {
            **_segment_audit_finding(segment, "dialogue"),
            "polish_applied": True,
            "rewrite_requested": False,
            "post_polish_seam_finding": post_polish_seam_finding,
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
        after_dialogue=final_dialogue,
        decision="accepted",
        error=None,
    )
    return True


def _should_recheck_seam_after_polish(
    *,
    segment: ScriptSegment,
    seam_finding: dict[str, Any] | None,
    intervention_targets: list[str],
) -> bool:
    if segment.scene_number <= 1 and any(
        isinstance(target, str) and target.startswith("seam:")
        for target in intervention_targets
    ):
        return True
    if not isinstance(seam_finding, dict):
        return False
    return (
        segment.scene_number == _safe_int(seam_finding.get("target_scene_number"), 0)
        and str(seam_finding.get("status") or "").strip() in PROBLEMATIC_SEAM_STATUSES
    )


async def _rerun_polished_scene_seam_audit(
    *,
    run_id: str,
    segment: ScriptSegment,
    language: str,
    scene_job: str,
    boundary_context: dict[str, Any] | None,
    seam_finding: dict[str, Any] | None,
    polished_dialogue: list[dict[str, Any]],
) -> dict[str, Any] | None:
    prompt_payload = _build_targeted_seam_audit_prompt_payload(
        segment=segment,
        language=language,
        scene_job=scene_job,
        boundary_context=boundary_context,
        seam_finding=seam_finding,
        polished_dialogue=polished_dialogue,
    )
    if prompt_payload is None:
        return None
    try:
        raw = await call_haiku_structured(
            system=SEAM_AUDIT_SYSTEM,
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            max_tokens=512,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        seams = _normalize_episode_seam_audit(parsed)
        report = seams[0] if seams else None
        _dump_dialogue_seam_debug(
            run_id=run_id,
            status="post_polish_ok",
            prompt_payload=prompt_payload,
            audit=seams,
            error=None,
        )
        return report
    except StepExecutionStopped:
        raise
    except Exception as exc:
        logger.warning(
            "Post-polish seam audit failed for act %s scene %s",
            segment.act_number,
            segment.scene_number,
            exc_info=True,
        )
        _dump_dialogue_seam_debug(
            run_id=run_id,
            status="post_polish_llm_or_parse_failed",
            prompt_payload=prompt_payload,
            audit=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None


def _build_dialogue_claim_sid(dialogue: list[dict[str, Any]] | None) -> ShortIdMap:
    return ShortIdMap(_extract_dialogue_claim_ids(dialogue or []), prefix="c")


def _shorten_dialogue_prompt_claim_ids(
    prompt_payload: dict[str, Any],
    claim_sid: ShortIdMap,
) -> dict[str, Any]:
    shortened = dict(prompt_payload)
    shortened["dialogue"] = shorten_dialogue_claim_ids(shortened.get("dialogue"), claim_sid)
    scenes = shortened.get("scenes")
    if isinstance(scenes, list):
        rewritten_scenes: list[dict[str, Any]] = []
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            copied = dict(scene)
            copied["dialogue"] = shorten_dialogue_claim_ids(copied.get("dialogue"), claim_sid)
            rewritten_scenes.append(copied)
        shortened["scenes"] = rewritten_scenes
    return shortened


def _shorten_multi_scene_prompt_claim_ids(
    prompt_payload: dict[str, Any],
    claim_sids_by_scene: dict[int, ShortIdMap],
) -> dict[str, Any]:
    shortened = dict(prompt_payload)
    scenes = shortened.get("scenes")
    if not isinstance(scenes, list):
        return shortened
    rewritten_scenes: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        copied = dict(scene)
        scene_number = _safe_int(copied.get("scene_number"), 0)
        claim_sid = claim_sids_by_scene.get(scene_number)
        if claim_sid is not None:
            copied["dialogue"] = shorten_dialogue_claim_ids(copied.get("dialogue"), claim_sid)
        rewritten_scenes.append(copied)
    shortened["scenes"] = rewritten_scenes
    return shortened


def _build_dialogue_polish_prompt_payload(
    *,
    segment: ScriptSegment,
    language: str,
    scene_job: str,
    scene_metadata: dict[str, Any] | None = None,
    boundary_context: dict[str, Any] | None,
    issues: list[dict[str, Any]],
    act_repetition_report: dict[str, Any] | None,
    seam_finding: dict[str, Any] | None = None,
    intervention_targets: list[str] | None = None,
) -> dict[str, Any]:
    targets = intervention_targets or []
    return {
        "act_number": segment.act_number,
        "scene_number": segment.scene_number,
        "language": str(language or "en").strip() or "en",
        "scene_job": scene_job,
        "scene_metadata": _dialogue_scene_metadata_prompt_excerpt(scene_metadata),
        "boundary_context": boundary_context,
        "act_repetition_report": act_repetition_report,
        "seam_finding": seam_finding,
        "intervention_targets": targets,
        "intervention_mode": _derive_dialogue_polish_intervention_mode(
            issues=issues,
            seam_finding=seam_finding,
            intervention_targets=targets,
        ),
        "success_criteria": _build_dialogue_polish_success_criteria(
            issues=issues,
            seam_finding=seam_finding,
            intervention_targets=targets,
            scene_job=scene_job,
        ),
        "issues": issues,
        "dialogue": segment.dialogue_json,
    }


def _build_multi_scene_polish_prompt_payload(
    *,
    candidate: dict[str, Any],
    language: str,
    act_repetition_report: dict[str, Any] | None,
) -> dict[str, Any]:
    scenes: list[dict[str, Any]] = []
    for row in candidate["rows"]:
        segment = row["segment"]
        scenes.append(
            {
                "scene_number": segment.scene_number,
                "scene_job": row.get("scene_job") or "",
                "boundary_context": row.get("boundary_context"),
                "issues": row.get("issues") or [],
                "intervention_targets": row.get("intervention_targets") or [],
                "dialogue": segment.dialogue_json,
            }
        )
    return {
        "act_number": candidate["act_number"],
        "language": str(language or "en").strip() or "en",
        "cluster_issue": {
            "family": candidate["family"],
            "severity": candidate["severity"],
            "scene_numbers": candidate["scene_numbers"],
            "description": candidate.get("description") or "",
        },
        "cluster_goal": "Differentiate the conversational move across these neighboring scenes without changing their facts or scene jobs.",
        "success_criteria": [
            "Preserve every scene's claim coverage, chronology, attribution, and declared scene job.",
            "Do not let all scenes open with the same skeptical-question reset.",
            "Increase opener variety across the cluster and reduce repeated explanatory resets.",
            "Keep the scenes in order and maintain delivery metadata compatibility.",
        ],
        "act_repetition_report": act_repetition_report,
        "scenes": scenes,
    }


def _derive_dialogue_polish_intervention_mode(
    *,
    issues: list[dict[str, Any]],
    seam_finding: dict[str, Any] | None,
    intervention_targets: list[str],
) -> str:
    if isinstance(seam_finding, dict) and str(seam_finding.get("status") or "").strip() in PROBLEMATIC_SEAM_STATUSES:
        return "seam_repair"
    if any(
        isinstance(issue, dict) and str(issue.get("type") or "").strip() in COMPREHENSION_ISSUE_TYPES
        for issue in issues
    ):
        return "clarity_repair"
    target_set = {target for target in intervention_targets if isinstance(target, str)}
    if target_set & {
        "local:scene_opening_echo",
        "local:boundary_restatement",
        "act:replay_question",
        "act:replay_punchline",
        "act:repeated_explanatory_reset",
    }:
        return "opener_repair"
    if target_set & {"local:repetitive_agreement_shape", "act:repeated_confirmation_first_opening"}:
        return "agreement_shape_repair"
    if target_set & {
        "local:rigid_turn_taking",
        "local:repetitive_challenge_shape",
        "local:teacherly_exposition",
        "act:repeated_interruption_first_opening",
        "act:speaker_distribution_imbalance",
    }:
        return "rhythm_repair"
    for issue in issues:
        if str(issue.get("scope") or "").strip() == "scene_polish":
            return "general_polish"
    return "general_polish"


def _build_dialogue_polish_success_criteria(
    *,
    issues: list[dict[str, Any]],
    seam_finding: dict[str, Any] | None,
    intervention_targets: list[str],
    scene_job: str,
) -> list[str]:
    criteria: list[str] = [
        "Preserve the scene job, claim coverage, chronology, and attribution.",
        "Keep the scene in the payload language and preserve delivery metadata compatibility.",
    ]
    if any(
        isinstance(issue, dict) and str(issue.get("type") or "").strip() in COMPREHENSION_ISSUE_TYPES
        for issue in issues
    ):
        criteria.append("Make the beat easier to follow in real time: clarify actors, causality, and any newly introduced term.")
    target_set = {target for target in intervention_targets if isinstance(target, str)}
    if isinstance(seam_finding, dict) and str(seam_finding.get("status") or "").strip() in PROBLEMATIC_SEAM_STATUSES:
        criteria.append("The opening should answer or sharpen the hanging question from the previous act instead of replaying it.")
    if target_set & {"local:scene_opening_echo", "local:boundary_restatement", "act:replay_question", "act:replay_punchline", "act:repeated_explanatory_reset"}:
        criteria.append("The first 1-2 turns must materially change, not just soften the old opener wording.")
    if target_set & {"local:repetitive_agreement_shape", "act:repeated_confirmation_first_opening"}:
        criteria.append("Break the confirmation loop by changing the move, not just swapping one confirmation marker for another.")
    if target_set & {"local:rigid_turn_taking", "local:repetitive_challenge_shape", "local:teacherly_exposition", "act:repeated_interruption_first_opening", "act:speaker_distribution_imbalance"}:
        criteria.append("Make the exchange less mechanical: vary who advances the idea and how the next turn moves.")
    if scene_job:
        criteria.append(f"Keep the scene anchored in its declared job: `{scene_job}`.")
    return criteria


def _build_multi_scene_polish_claim_sids(rows: list[dict[str, Any]]) -> dict[int, ShortIdMap]:
    claim_sids: dict[int, ShortIdMap] = {}
    for row in rows:
        segment = row.get("segment")
        if segment is None:
            continue
        scene_number = _safe_int(getattr(segment, "scene_number", 0), 0)
        if scene_number <= 0:
            continue
        claim_ids = _extract_dialogue_claim_ids(segment.dialogue_json)
        claim_sids[scene_number] = ShortIdMap(_dedupe_preserve_order(claim_ids), prefix="c")
    return claim_sids


def _build_dialogue_audit_prompt_payload(
    *,
    segment: ScriptSegment,
    language: str,
    boundary_context: dict[str, Any] | None,
    scene_job: str,
    scene_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "act_number": segment.act_number,
        "scene_number": segment.scene_number,
        "language": str(language or "en").strip() or "en",
        "scene_job": scene_job,
        "boundary_context": boundary_context,
        "scene_metadata": _dialogue_scene_metadata_prompt_excerpt(scene_metadata),
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
    deduped: dict[str, dict[str, Any]] = {}
    first_index_by_type: dict[str, int] = {}
    for idx, item in enumerate(value):
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
        issue_type_value = issue_type.strip()
        normalized_issue = {
            "type": issue_type_value,
            "severity": severity,
            "scope": scope,
            "description": _compact_audit_text(description),
            "suggestion": _compact_audit_text(suggestion),
        }
        if issue_type_value not in first_index_by_type:
            first_index_by_type[issue_type_value] = idx
            deduped[issue_type_value] = normalized_issue
            continue
        existing_issue = deduped[issue_type_value]
        existing_rank = (
            _severity_rank(str(existing_issue.get("severity"))),
            _repair_scope_rank(str(existing_issue.get("scope"))),
        )
        new_rank = (
            _severity_rank(severity),
            _repair_scope_rank(scope),
        )
        if new_rank > existing_rank:
            deduped[issue_type_value] = normalized_issue

    ordered = sorted(
        deduped.items(),
        key=lambda item: (
            -_severity_rank(str(item[1].get("severity"))),
            -_repair_scope_rank(str(item[1].get("scope"))),
            first_index_by_type.get(item[0], 0),
        ),
    )
    return [issue for _, issue in ordered[:MAX_AUDIT_ISSUES]]


def _compact_audit_text(value: Any, *, max_chars: int = MAX_AUDIT_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rstrip()
    if " " not in truncated:
        return truncated
    return truncated.rsplit(" ", 1)[0]


def _severity_rank(value: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(value, 0)


def _repair_scope_rank(value: str) -> int:
    return {"act_warning": 1, "scene_polish": 2, "scene_rewrite": 3}.get(value, 0)


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


def _line_is_question(text: Any) -> bool:
    return isinstance(text, str) and text.rstrip().endswith("?")


def _opener_signature(dialogue: Any) -> tuple[str, str]:
    line = _first_valid_line(dialogue)
    if not isinstance(line, dict):
        return ("", "")
    return (
        str(line.get("speaker") or "").strip(),
        _normalized_text_signature(line.get("text")),
    )


def _dialogue_turn_pattern(dialogue: Any) -> str:
    if not isinstance(dialogue, list):
        return ""
    pattern: list[str] = []
    for item in dialogue:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = str(item.get("speaker") or "").strip()
        if speaker:
            pattern.append(speaker)
    return "".join(pattern)


def _opening_exchange_shape(dialogue: Any) -> str:
    if not isinstance(dialogue, list):
        return ""
    valid_lines = [line for line in dialogue if isinstance(line, dict) and str(line.get("text") or "").strip()]
    if len(valid_lines) < 2:
        return ""
    first = valid_lines[0]
    second = valid_lines[1]
    first_speaker = str(first.get("speaker") or "").strip()
    second_speaker = str(second.get("speaker") or "").strip()
    if not first_speaker or not second_speaker:
        return ""
    first_question = _line_is_question(str(first.get("text") or ""))
    if first_speaker == "B" and second_speaker == "A" and first_question:
        return "B_question_to_A"
    return f"{first_speaker}_{'question' if first_question else 'statement'}_to_{second_speaker}"


def _is_explanatory_reset_opening(dialogue: Any) -> bool:
    if not isinstance(dialogue, list):
        return False
    valid_lines = [line for line in dialogue if isinstance(line, dict) and str(line.get("text") or "").strip()]
    if len(valid_lines) < 4:
        return False
    speakers = [str(line.get("speaker") or "").strip() for line in valid_lines[:4]]
    if speakers != ["B", "A", "B", "A"]:
        return False
    texts = [str(line.get("text") or "").strip() for line in valid_lines[:4]]
    return _line_is_question(texts[0]) and not _line_is_question(texts[1]) and not _line_is_question(texts[3])


def _opener_shape(dialogue: Any) -> str:
    line = _first_valid_line(dialogue)
    if not isinstance(line, dict):
        return ""
    speaker = str(line.get("speaker") or "").strip()
    text = str(line.get("text") or "").strip()
    if not speaker or not text:
        return ""
    exchange_shape = _opening_exchange_shape(dialogue) or "single_turn"
    return f"{speaker}:{'question' if _line_is_question(text) else 'statement'}:{exchange_shape}"


def _dialogue_cluster_rhythm_metrics(dialogues: list[Any]) -> dict[str, Any]:
    opener_shapes = [_opener_shape(dialogue) for dialogue in dialogues if _opener_shape(dialogue)]
    opening_speakers = [_dialogue_edge_speaker(dialogue, edge="first") for dialogue in dialogues if _dialogue_edge_speaker(dialogue, edge="first")]
    opening_exchange_shapes = [_opening_exchange_shape(dialogue) for dialogue in dialogues if _opening_exchange_shape(dialogue)]

    opener_counts = Counter(opener_shapes)
    opening_speaker_counts = Counter(opening_speakers)
    scene_count = max(len(dialogues), 1)
    dominant_opening_speaker_count = max(opening_speaker_counts.values(), default=0)
    return {
        "scene_count": len(dialogues),
        "distinct_opener_shapes": len(opener_counts),
        "max_repeated_opener_shape": max(opener_counts.values(), default=0),
        "b_question_a_openings": sum(
            1 for shape in opening_exchange_shapes if shape == "B_question_to_A"
        ),
        "explanatory_reset_openings": sum(1 for dialogue in dialogues if _is_explanatory_reset_opening(dialogue)),
        "dominant_opening_speaker_share": round(dominant_opening_speaker_count / scene_count, 3),
    }


def _build_dialogue_rhythm_metrics(segments: list[ScriptSegment]) -> dict[str, Any]:
    dialogues = [segment.dialogue_json for segment in segments]
    lines = [
        line
        for segment in segments
        for line in (segment.dialogue_json or [])
        if isinstance(line, dict) and str(line.get("text") or "").strip()
    ]
    opening_lines = [
        line
        for segment in segments
        if isinstance((line := _first_valid_line(segment.dialogue_json)), dict)
    ]
    speaker_question_share = _speaker_question_share(lines)
    cluster_metrics = _dialogue_cluster_rhythm_metrics(dialogues)
    return {
        "scene_count": len(segments),
        "line_count": len(lines),
        "opening_speaker_counts": dict(
            Counter(
                _dialogue_edge_speaker(segment.dialogue_json, edge="first")
                for segment in segments
                if _dialogue_edge_speaker(segment.dialogue_json, edge="first")
            )
        ),
        "speaker_question_share": speaker_question_share,
        "dominant_opening_speaker_share": cluster_metrics["dominant_opening_speaker_share"],
        "scene_opening_question_rate": round(
            sum(1 for line in opening_lines if _line_is_question(str(line.get("text") or ""))) / max(len(opening_lines), 1),
            3,
        ),
        "opening_shape_diversity": cluster_metrics["distinct_opener_shapes"],
        "opening_shape_diversity_rate": round(
            cluster_metrics["distinct_opener_shapes"] / max(len(opening_lines), 1),
            3,
        ),
        "b_question_a_opening_rate": round(
            cluster_metrics["b_question_a_openings"] / max(len(opening_lines), 1),
            3,
        ),
        "explanatory_reset_opening_rate": round(
            cluster_metrics["explanatory_reset_openings"] / max(len(opening_lines), 1),
            3,
        ),
    }


def _build_dialogue_rhythm_metrics_by_act(segments: list[ScriptSegment]) -> dict[str, dict[str, Any]]:
    by_act: dict[str, list[ScriptSegment]] = {}
    for segment in segments:
        by_act.setdefault(_act_count_key(segment.act_number), []).append(segment)
    return {
        act_key: _build_dialogue_rhythm_metrics(sorted(act_segments, key=lambda item: item.scene_number))
        for act_key, act_segments in by_act.items()
    }


def _speaker_question_share(lines: list[dict[str, Any]]) -> dict[str, float]:
    per_speaker: dict[str, Counter[str]] = {}
    for line in lines:
        speaker = str(line.get("speaker") or "").strip()
        text = str(line.get("text") or "").strip()
        if not speaker or not text:
            continue
        counter = per_speaker.setdefault(speaker, Counter())
        counter["lines"] += 1
        if _line_is_question(text):
            counter["questions"] += 1
    return {
        speaker: round(counter["questions"] / max(counter["lines"], 1), 3)
        for speaker, counter in per_speaker.items()
    }


def _dialogue_has_delivery_metadata(dialogue: Any) -> bool:
    if not isinstance(dialogue, list):
        return False
    for line in dialogue:
        if not isinstance(line, dict):
            continue
        if "emotion" in line or "ssml_hints" in line:
            return True
    return False


def _finalize_polished_dialogue(
    *,
    original_dialogue: list[dict[str, Any]],
    polished_dialogue: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not polished_dialogue:
        return None

    if len(polished_dialogue) == len([line for line in original_dialogue if isinstance(line, dict)]):
        merged_dialogue = _merge_dialogue_metadata(
            original_dialogue=original_dialogue,
            repaired_dialogue=polished_dialogue,
        )
        if merged_dialogue:
            return merged_dialogue

    if _dialogue_has_delivery_metadata(original_dialogue):
        if not _dialogue_has_delivery_metadata(polished_dialogue):
            return None
    return polished_dialogue


def _requires_material_opener_change(issues: list[dict[str, Any]]) -> bool:
    return any(
        issue.get("type") in ECHO_ISSUE_TYPES
        and issue.get("scope") == "scene_polish"
        and _severity_rank(str(issue.get("severity"))) >= 2
        for issue in issues
    )


def _opener_changed_materially(original_dialogue: Any, polished_dialogue: Any) -> bool:
    return _opener_signature(original_dialogue) != _opener_signature(polished_dialogue)


def _dialogue_changed_materially(original_dialogue: Any, polished_dialogue: Any) -> bool:
    def _signature(dialogue: Any) -> list[tuple[str, str]]:
        if not isinstance(dialogue, list):
            return []
        signature: list[tuple[str, str]] = []
        for line in dialogue:
            if not isinstance(line, dict):
                continue
            text = line.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            signature.append(
                (
                    str(line.get("speaker") or "").strip(),
                    _normalized_text_signature(text),
                )
            )
        return signature

    return _signature(original_dialogue) != _signature(polished_dialogue)


def _build_act_dialogue_reports(audit_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    issue_scene_numbers_by_act: dict[str, dict[str, list[int]]] = {}
    turn_pattern_counts_by_act: dict[str, dict[str, int]] = {}
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
                "turn_pattern_counts": {},
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

        turn_pattern = _dialogue_turn_pattern(segment.dialogue_json)
        if turn_pattern:
            act_turn_patterns = turn_pattern_counts_by_act.setdefault(act_key, {})
            act_turn_patterns[turn_pattern] = act_turn_patterns.get(turn_pattern, 0) + 1

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
        report["turn_pattern_counts"] = dict(turn_pattern_counts_by_act.get(act_key, {}))
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
        report["rhythm_metrics"] = _build_dialogue_rhythm_metrics(
            [
                row["segment"]
                for row in sorted(
                    (
                        row
                        for row in audit_rows
                        if _act_count_key(row["segment"].act_number) == act_key
                    ),
                    key=lambda item: item["segment"].scene_number,
                )
            ]
        )
    _refresh_dialogue_priorities(audit_rows, reports)
    return reports


def _dialogue_priority_score(row: dict[str, Any], act_report: dict[str, Any]) -> int:
    issues = row.get("issues") or []

    score = 0
    for issue in issues:
        issue_type = str(issue.get("type") or "").strip()
        severity_rank = _severity_rank(str(issue.get("severity")))
        scope = str(issue.get("scope") or "")
        if scope == "scene_rewrite":
            score += 80 + severity_rank * 10
            continue
        if issue_type in COMPREHENSION_ISSUE_TYPES:
            score += 34 + severity_rank * 9
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
    llm_priority_scene_numbers = {
        _safe_int(scene_number, 0)
        for scene_number in act_report.get("llm_priority_scene_numbers", [])
        if _safe_int(scene_number, 0) > 0
    }
    if segment.scene_number in llm_priority_scene_numbers:
        score += 16

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
        family = str(finding.get("family") or "").strip()
        severity_score = 5 * max(1, _severity_rank(str(finding.get("severity"))))
        if family in {"replay_question", "replay_punchline"}:
            score += severity_score + 12
        elif family == "speaker_distribution_imbalance":
            score += severity_score + 8
        else:
            score += severity_score + 4

    seam_finding = row.get("seam_finding")
    if isinstance(seam_finding, dict):
        seam_status = str(seam_finding.get("status") or "").strip()
        seam_severity = _severity_rank(str(seam_finding.get("severity")))
        if seam_status in PROBLEMATIC_SEAM_STATUSES:
            score += 24 + seam_severity * 8

    intervention_targets = row.get("intervention_targets") or []
    if isinstance(intervention_targets, list):
        for target in intervention_targets:
            if not isinstance(target, str):
                continue
            if target.startswith("seam:"):
                score += 8
            elif target.startswith("act:"):
                score += 6
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
            and (_row_should_polish_dialogue(row) or _should_request_scene_rewrite(row["issues"]))
        ]
        prioritized.extend(
            sorted(
                act_rows,
                key=lambda row: (-int(row.get("priority_score", 0)), row["segment"].scene_number),
            )
        )
    return prioritized


def _row_should_polish_dialogue(row: dict[str, Any]) -> bool:
    if _should_polish_dialogue(row.get("issues") or []):
        return True
    if bool(row.get("llm_priority_selected")):
        return True
    act_target_present = False
    for target in row.get("intervention_targets") or []:
        if not isinstance(target, str):
            continue
        if target.startswith("seam:"):
            return True
        if target.startswith("act:"):
            act_target_present = True
    if act_target_present:
        return bool(row.get("llm_priority_selected"))
    return False


def _refresh_dialogue_priorities(
    audit_rows: list[dict[str, Any]],
    act_reports: dict[str, dict[str, Any]],
) -> None:
    for row in audit_rows:
        act_key = _act_count_key(row["segment"].act_number)
        act_report = act_reports.get(act_key, {})
        llm_priority_scene_numbers = {
            _safe_int(scene_number, 0)
            for scene_number in act_report.get("llm_priority_scene_numbers", [])
            if _safe_int(scene_number, 0) > 0
        }
        row["llm_priority_selected"] = row["segment"].scene_number in llm_priority_scene_numbers
        row["priority_score"] = _dialogue_priority_score(row, act_report)

    for act_key, report in act_reports.items():
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
        "turn_pattern_counts": act_report.get("turn_pattern_counts"),
        "repetition_families": act_report.get("repetition_families"),
        "priority_scene_numbers": act_report.get("priority_scene_numbers"),
        "distribution_findings": act_report.get("distribution_findings"),
        "monotony_signals": _dialogue_act_monotony_signals(act_report),
        "rhythm_metrics": act_report.get("rhythm_metrics"),
    }


def _dialogue_act_monotony_signals(act_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(act_report, dict):
        return []
    signals: list[dict[str, Any]] = []
    for finding in act_report.get("distribution_findings", []) or []:
        if not isinstance(finding, dict):
            continue
        family = str(finding.get("family") or "").strip()
        if family not in ACT_MONOTONY_ROUTING_FAMILIES:
            continue
        scene_numbers = [
            _safe_int(scene_number, 0)
            for scene_number in finding.get("scene_numbers", [])
            if _safe_int(scene_number, 0) > 0
        ]
        if len(scene_numbers) < 2:
            continue
        signals.append(
            {
                "family": family,
                "severity": str(finding.get("severity") or "medium").strip().lower(),
                "scene_numbers": scene_numbers,
                "description": str(finding.get("description") or "").strip(),
            }
        )
    return signals


def _build_multi_scene_polish_candidates(
    *,
    audit_rows: list[dict[str, Any]],
    act_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_act_scene: dict[str, dict[int, dict[str, Any]]] = {}
    for row in audit_rows:
        segment = row.get("segment")
        if segment is None:
            continue
        rows_by_act_scene.setdefault(_act_count_key(segment.act_number), {})[segment.scene_number] = row

    candidates: list[dict[str, Any]] = []
    for act_key, act_report in act_reports.items():
        act_rows = rows_by_act_scene.get(act_key, {})
        for signal in _dialogue_act_monotony_signals(act_report):
            if signal["family"] not in MULTI_SCENE_POLISH_FAMILIES:
                continue
            contiguous_runs = _contiguous_scene_runs(signal["scene_numbers"])
            selected_run = next(
                (
                    run[:MAX_MULTI_SCENE_CLUSTER_SIZE]
                    for run in contiguous_runs
                    if len(run) >= MIN_MULTI_SCENE_CLUSTER_SIZE
                ),
                None,
            )
            if not selected_run:
                continue
            rows = [act_rows.get(scene_number) for scene_number in selected_run]
            if any(row is None for row in rows):
                continue
            candidate_rows = [row for row in rows if isinstance(row, dict)]
            if len(candidate_rows) < MIN_MULTI_SCENE_CLUSTER_SIZE:
                continue
            if not all(bool(row.get("can_intervene")) for row in candidate_rows):
                continue
            candidate = {
                "act_number": _safe_int(act_report.get("act_number"), 0),
                "family": signal["family"],
                "severity": signal["severity"],
                "scene_numbers": selected_run,
                "description": signal["description"],
                "rows": candidate_rows,
            }
            candidates.append(candidate)
            for row in candidate_rows:
                row["multi_scene_candidate"] = {
                    "family": signal["family"],
                    "scene_numbers": selected_run,
                    "severity": signal["severity"],
                }
            break
    return candidates


def _contiguous_scene_runs(scene_numbers: list[int]) -> list[list[int]]:
    ordered = sorted({number for number in scene_numbers if isinstance(number, int) and number > 0})
    if not ordered:
        return []
    runs: list[list[int]] = [[ordered[0]]]
    for scene_number in ordered[1:]:
        current = runs[-1]
        if scene_number == current[-1] + 1:
            current.append(scene_number)
            continue
        runs.append([scene_number])
    return runs


async def _attempt_multi_scene_polish(
    *,
    db,
    run_id: str,
    candidate: dict[str, Any],
    language: str,
    speaker_aliases: dict[str, str] | None,
    act_repetition_report: dict[str, Any] | None,
) -> bool:
    rows = candidate["rows"]
    original_dialogues = {
        row["segment"].scene_number: row["segment"].dialogue_json
        for row in rows
    }
    claim_sids_by_scene = _build_multi_scene_polish_claim_sids(rows)
    prompt_payload = _build_multi_scene_polish_prompt_payload(
        candidate=candidate,
        language=language,
        act_repetition_report=act_repetition_report,
    )
    prompt_payload = _shorten_multi_scene_prompt_claim_ids(prompt_payload, claim_sids_by_scene)

    try:
        raw = await call_sonnet_structured(
            system=MULTI_SCENE_POLISH_SYSTEM,
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            max_tokens=4096,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        polished_by_scene = _normalize_multi_scene_polish_output(
            parsed.get("scenes"),
            expected_scene_numbers=candidate["scene_numbers"],
            speaker_aliases=speaker_aliases,
            claim_sids_by_scene=claim_sids_by_scene,
        )
    except StepExecutionStopped:
        raise
    except Exception as exc:
        _dump_multi_scene_polish_debug(
            run_id=run_id,
            candidate=candidate,
            status="llm_or_parse_failed",
            payload=prompt_payload,
            before_dialogues=original_dialogues,
            after_dialogues=None,
            decision="rejected",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False

    if not polished_by_scene:
        _dump_multi_scene_polish_debug(
            run_id=run_id,
            candidate=candidate,
            status="empty_dialogue",
            payload=prompt_payload,
            before_dialogues=original_dialogues,
            after_dialogues=polished_by_scene,
            decision="rejected",
            error="multi-scene polish produced no usable dialogue",
        )
        return False

    final_by_scene: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        segment = row["segment"]
        scene_number = segment.scene_number
        polished_dialogue = polished_by_scene.get(scene_number)
        if not polished_dialogue:
            _dump_multi_scene_polish_debug(
                run_id=run_id,
                candidate=candidate,
                status="scene_missing",
                payload=prompt_payload,
                before_dialogues=original_dialogues,
                after_dialogues=polished_by_scene,
                decision="rejected",
                error=f"multi-scene polish did not return scene {scene_number}",
            )
            return False
        original_claim_ids = set(_extract_dialogue_claim_ids(segment.dialogue_json))
        polished_claim_ids = set(_extract_dialogue_claim_ids(polished_dialogue))
        if original_claim_ids and not original_claim_ids.issubset(polished_claim_ids):
            _dump_multi_scene_polish_debug(
                run_id=run_id,
                candidate=candidate,
                status="claim_coverage_regressed",
                payload=prompt_payload,
                before_dialogues=original_dialogues,
                after_dialogues=polished_by_scene,
                decision="rejected",
                error=f"multi-scene polish dropped required claim ids for scene {scene_number}",
            )
            return False
        if polished_claim_ids and not polished_claim_ids.issubset(original_claim_ids):
            _dump_multi_scene_polish_debug(
                run_id=run_id,
                candidate=candidate,
                status="claim_scope_expanded",
                payload=prompt_payload,
                before_dialogues=original_dialogues,
                after_dialogues=polished_by_scene,
                decision="rejected",
                error=f"multi-scene polish introduced borrowed claim ids in scene {scene_number}",
            )
            return False
        final_dialogue = _finalize_polished_dialogue(
            original_dialogue=segment.dialogue_json,
            polished_dialogue=polished_dialogue,
        )
        if not final_dialogue:
            _dump_multi_scene_polish_debug(
                run_id=run_id,
                candidate=candidate,
                status="metadata_contract_failed",
                payload=prompt_payload,
                before_dialogues=original_dialogues,
                after_dialogues=polished_by_scene,
                decision="rejected",
                error=f"multi-scene polish did not preserve delivery metadata safely for scene {scene_number}",
            )
            return False
        final_by_scene[scene_number] = final_dialogue

    if not _multi_scene_polish_resolves_targets(
        family=str(candidate.get("family") or "").strip(),
        original_by_scene=original_dialogues,
        polished_by_scene=final_by_scene,
    ):
        _dump_multi_scene_polish_debug(
            run_id=run_id,
            candidate=candidate,
            status="target_not_resolved",
            payload=prompt_payload,
            before_dialogues=original_dialogues,
            after_dialogues=final_by_scene,
            decision="rejected",
            error="multi-scene polish changed text but did not improve cluster-level rhythm enough",
        )
        return False

    for row in rows:
        segment = row["segment"]
        scene_number = segment.scene_number
        segment.dialogue_json = final_by_scene[scene_number]
        segment.revision_count += 1
        _set_segment_audit_finding(
            segment,
            "dialogue",
            {
                **_segment_audit_finding(segment, "dialogue"),
                "multi_scene_polish_applied": True,
                "multi_scene_cluster": {
                    "family": candidate["family"],
                    "scene_numbers": candidate["scene_numbers"],
                },
            },
        )
    await db.flush()
    _dump_multi_scene_polish_debug(
        run_id=run_id,
        candidate=candidate,
        status="accepted",
        payload=prompt_payload,
        before_dialogues=original_dialogues,
        after_dialogues=final_by_scene,
        decision="accepted",
        error=None,
    )
    return True


def _normalize_multi_scene_polish_output(
    value: Any,
    *,
    expected_scene_numbers: list[int],
    speaker_aliases: dict[str, str] | None,
    claim_sids_by_scene: dict[int, ShortIdMap],
) -> dict[int, list[dict[str, Any]]] | None:
    if not isinstance(value, list):
        return None
    expected = set(expected_scene_numbers)
    normalized: dict[int, list[dict[str, Any]]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        scene_number = _safe_int(item.get("scene_number"), 0)
        if scene_number not in expected or scene_number in normalized:
            continue
        dialogue = _normalize_dialogue_output(
            item.get("dialogue"),
            speaker_aliases=speaker_aliases,
        )
        claim_sid = claim_sids_by_scene.get(scene_number)
        if claim_sid is not None:
            dialogue = restore_dialogue_claim_ids(dialogue, claim_sid)
        if dialogue:
            normalized[scene_number] = dialogue
    if set(normalized) != expected:
        return None
    return normalized


def _multi_scene_polish_resolves_targets(
    *,
    family: str,
    original_by_scene: dict[int, list[dict[str, Any]]],
    polished_by_scene: dict[int, list[dict[str, Any]]],
) -> bool:
    changed_scene_count = sum(
        1
        for scene_number, original_dialogue in original_by_scene.items()
        if _dialogue_changed_materially(original_dialogue, polished_by_scene.get(scene_number))
    )
    if changed_scene_count < 2:
        return False

    before_metrics = _dialogue_cluster_rhythm_metrics(list(original_by_scene.values()))
    after_metrics = _dialogue_cluster_rhythm_metrics(
        [polished_by_scene[scene_number] for scene_number in sorted(polished_by_scene)]
    )

    if (
        family == "repeated_explanatory_reset"
        and before_metrics["explanatory_reset_openings"] > 0
    ):
        if after_metrics["explanatory_reset_openings"] >= before_metrics["explanatory_reset_openings"]:
            return False
        if after_metrics["explanatory_reset_openings"] > max(0, before_metrics["scene_count"] // 2):
            return False

    return any(
        [
            after_metrics["distinct_opener_shapes"] > before_metrics["distinct_opener_shapes"],
            after_metrics["max_repeated_opener_shape"] < before_metrics["max_repeated_opener_shape"],
            after_metrics["b_question_a_openings"] < before_metrics["b_question_a_openings"],
            after_metrics["explanatory_reset_openings"] < before_metrics["explanatory_reset_openings"],
            after_metrics["dominant_opening_speaker_share"] < before_metrics["dominant_opening_speaker_share"],
        ]
    )


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
    _refresh_dialogue_priorities(audit_rows, augmented)
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
        scene_summaries.append(
            {
                "scene_number": segment.scene_number,
                "scene_job": row.get("scene_job") or "",
                "opening_speaker": _dialogue_edge_speaker(segment.dialogue_json, edge="first"),
                "closing_speaker": _dialogue_edge_speaker(segment.dialogue_json, edge="last"),
                "turn_pattern": _dialogue_turn_pattern(segment.dialogue_json),
                "line_count": len([line for line in segment.dialogue_json if isinstance(line, dict)]),
                "issue_types": [str(issue.get("type") or "").strip() for issue in row.get("issues", []) if issue],
                "priority_score": row.get("priority_score", 0),
                "dialogue": segment.dialogue_json,
            }
        )
    return {
        "language": str(language or "en").strip() or "en",
        "act_number": act_report.get("act_number"),
        "structured_context": {
            "scene_count": act_report.get("scene_count"),
            "opening_speaker_counts": act_report.get("opening_speaker_counts"),
            "closing_speaker_counts": act_report.get("closing_speaker_counts"),
            "issue_counts": act_report.get("issue_counts"),
            "turn_pattern_counts": act_report.get("turn_pattern_counts"),
            "rhythm_metrics": act_report.get("rhythm_metrics"),
            "heuristic_repetition_families": act_report.get("repetition_families"),
        },
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
            if family not in ACT_FINDING_FAMILIES:
                family = "other"
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
                    "family": family,
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


def _build_episode_seam_audit_prompt_payload(
    *,
    segments: list[ScriptSegment],
    scene_job_lookup: dict[tuple[int, int], str],
    language: str,
) -> dict[str, Any] | None:
    seam_cards: list[dict[str, Any]] = []
    segments_by_act: dict[int, list[ScriptSegment]] = {}
    for segment in segments:
        segments_by_act.setdefault(segment.act_number, []).append(segment)

    ordered_acts = sorted(segments_by_act)
    for previous_act, next_act in zip(ordered_acts, ordered_acts[1:], strict=False):
        previous_segments = sorted(segments_by_act.get(previous_act, []), key=lambda item: item.scene_number)
        next_segments = sorted(segments_by_act.get(next_act, []), key=lambda item: item.scene_number)
        if not previous_segments or not next_segments:
            continue
        previous_scene = previous_segments[-1]
        next_scene = next_segments[0]
        previous_tail = _extract_dialogue_texts(previous_scene.dialogue_json)[-3:]
        next_opening = _extract_dialogue_texts(next_scene.dialogue_json)[:3]
        if not previous_tail or not next_opening:
            continue
        seam_cards.append(
            {
                "from_act_number": previous_act,
                "from_scene_number": previous_scene.scene_number,
                "from_scene_job": scene_job_lookup.get((previous_scene.act_number, previous_scene.scene_number), ""),
                "to_act_number": next_act,
                "target_scene_number": next_scene.scene_number,
                "to_scene_job": scene_job_lookup.get((next_scene.act_number, next_scene.scene_number), ""),
                "previous_scene_tail": previous_tail,
                "next_scene_opening": next_opening,
            }
        )

    if not seam_cards:
        return None

    return {
        "language": str(language or "en").strip() or "en",
        "seams": seam_cards,
    }


def _build_targeted_seam_audit_prompt_payload(
    *,
    segment: ScriptSegment,
    language: str,
    scene_job: str,
    boundary_context: dict[str, Any] | None,
    seam_finding: dict[str, Any] | None,
    polished_dialogue: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(boundary_context, dict):
        return None
    previous_tail = boundary_context.get("last_lines")
    if not isinstance(previous_tail, list):
        return None
    previous_tail = [str(line).strip() for line in previous_tail if isinstance(line, str) and line.strip()]
    next_opening = _extract_dialogue_texts(polished_dialogue)[:3]
    if not previous_tail or not next_opening:
        return None

    return {
        "language": str(language or "en").strip() or "en",
        "seams": [
            {
                "from_act_number": _safe_int(boundary_context.get("act_number"), segment.act_number - 1),
                "from_scene_number": _safe_int(boundary_context.get("scene_number"), 0),
                "from_scene_job": (
                    str(seam_finding.get("from_scene_job") or "").strip()
                    if isinstance(seam_finding, dict)
                    else ""
                ),
                "to_act_number": segment.act_number,
                "target_scene_number": segment.scene_number,
                "to_scene_job": scene_job,
                "previous_scene_tail": previous_tail,
                "next_scene_opening": next_opening,
            }
        ],
    }


def _normalize_seam_status(value: Any) -> str:
    status = str(value or "").strip()
    if status in SEAM_STATUSES:
        return status
    return "acceptable_handoff"


def _normalize_episode_seam_audit(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    seams: list[dict[str, Any]] = []
    raw_seams = value.get("seams")
    if not isinstance(raw_seams, list):
        return []
    for item in raw_seams:
        if not isinstance(item, dict):
            continue
        from_act_number = _safe_int(item.get("from_act_number"), 0)
        to_act_number = _safe_int(item.get("to_act_number"), 0)
        target_scene_number = _safe_int(item.get("target_scene_number"), 0)
        if from_act_number <= 0 or to_act_number <= 0 or target_scene_number <= 0:
            continue
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in ISSUE_SEVERITIES:
            severity = "medium"
        seams.append(
            {
                "from_act_number": from_act_number,
                "to_act_number": to_act_number,
                "target_scene_number": target_scene_number,
                "status": _normalize_seam_status(item.get("status")),
                "severity": severity,
                "description": str(item.get("description") or "").strip(),
            }
        )
    return seams


async def _run_episode_seam_audit(
    *,
    segments: list[ScriptSegment],
    scene_job_lookup: dict[tuple[int, int], str],
    language: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    prompt_payload = _build_episode_seam_audit_prompt_payload(
        segments=segments,
        scene_job_lookup=scene_job_lookup,
        language=language,
    )
    if prompt_payload is None:
        return []
    try:
        raw = await call_haiku_structured(
            system=SEAM_AUDIT_SYSTEM,
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            max_tokens=1024,
            temperature=0.2,
        )
        parsed = parse_llm_json(raw)
        seams = _normalize_episode_seam_audit(parsed)
        _dump_dialogue_seam_debug(
            run_id=run_id or "unknown",
            status="ok",
            prompt_payload=prompt_payload,
            audit=seams,
            error=None,
        )
        return seams
    except StepExecutionStopped:
        raise
    except Exception as exc:
        logger.warning("Episode seam audit failed", exc_info=True)
        _dump_dialogue_seam_debug(
            run_id=run_id or "unknown",
            status="llm_or_parse_failed",
            prompt_payload=prompt_payload,
            audit=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return []


def _seam_reports_by_target(seam_reports: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    mapped: dict[tuple[int, int], dict[str, Any]] = {}
    for report in seam_reports:
        if not isinstance(report, dict):
            continue
        act_number = _safe_int(report.get("to_act_number"), 0)
        scene_number = _safe_int(report.get("target_scene_number"), 0)
        if act_number <= 0 or scene_number <= 0:
            continue
        mapped[(act_number, scene_number)] = report
    return mapped


def _finalize_dialogue_seam_reports(
    *,
    seam_reports: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_target = _seam_reports_by_target(seam_reports)
    for row in audit_rows:
        segment = row.get("segment")
        if segment is None:
            continue
        post_polish = _segment_audit_finding(segment, "dialogue").get("post_polish_seam_finding")
        if not isinstance(post_polish, dict):
            continue
        act_number = _safe_int(getattr(segment, "act_number", 0), 0)
        scene_number = _safe_int(getattr(segment, "scene_number", 0), 0)
        if act_number <= 0 or scene_number <= 0:
            continue
        by_target[(act_number, scene_number)] = post_polish
    return list(by_target.values())


def _dialogue_intervention_targets(
    *,
    row: dict[str, Any],
    act_report: dict[str, Any],
    seam_finding: dict[str, Any] | None,
) -> list[str]:
    targets: list[str] = []
    for issue in row.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issue_type = str(issue.get("type") or "").strip()
        scope = str(issue.get("scope") or "").strip()
        if not issue_type or scope not in {"scene_polish", "scene_rewrite"}:
            continue
        targets.append(f"local:{issue_type}")

    segment = row.get("segment")
    scene_number = _safe_int(getattr(segment, "scene_number", 0), 0)
    for finding in act_report.get("distribution_findings", []) or []:
        if not isinstance(finding, dict):
            continue
        scene_numbers = {
            _safe_int(scene_number_value, 0)
            for scene_number_value in finding.get("scene_numbers", [])
            if _safe_int(scene_number_value, 0) > 0
        }
        if scene_number and scene_number in scene_numbers:
            family = str(finding.get("family") or "").strip()
            if family:
                targets.append(f"act:{family}")

    if isinstance(seam_finding, dict):
        status = str(seam_finding.get("status") or "").strip()
        if status in PROBLEMATIC_SEAM_STATUSES:
            targets.append(f"seam:{status}")

    return _dedupe_preserve_order(targets)


def _intervention_requires_opener_change(
    *,
    issues: list[dict[str, Any]],
    seam_finding: dict[str, Any] | None,
    intervention_targets: list[str],
) -> bool:
    if _requires_material_opener_change(issues):
        return True
    if isinstance(seam_finding, dict) and str(seam_finding.get("status") or "").strip() in PROBLEMATIC_SEAM_STATUSES:
        return True
    for target in intervention_targets:
        if not isinstance(target, str):
            continue
        if target.startswith("seam:"):
            return True
        if target in {
            "act:replay_question",
            "act:replay_punchline",
            "act:repeated_explanatory_reset",
            "act:repeated_confirmation_first_opening",
            "act:repeated_interruption_first_opening",
            "act:speaker_distribution_imbalance",
            "local:scene_opening_echo",
            "local:boundary_restatement",
        }:
            return True
    return False


def _dialogue_polish_resolves_targets(
    *,
    original_dialogue: Any,
    polished_dialogue: Any,
    issues: list[dict[str, Any]],
    boundary_context: dict[str, Any] | None,
    act_repetition_report: dict[str, Any] | None,
    seam_finding: dict[str, Any] | None,
    intervention_targets: list[str],
    scene_metadata: dict[str, Any] | None = None,
) -> bool:
    if not issues and not isinstance(seam_finding, dict) and not intervention_targets:
        return _dialogue_changed_materially(original_dialogue, polished_dialogue)

    has_comprehension_issue = any(
        isinstance(issue, dict) and str(issue.get("type") or "").strip() in COMPREHENSION_ISSUE_TYPES
        for issue in issues
    )

    if not _intervention_requires_opener_change(
        issues=issues,
        seam_finding=seam_finding,
        intervention_targets=intervention_targets,
    ):
        if not _dialogue_changed_materially(original_dialogue, polished_dialogue):
            return False
        if has_comprehension_issue:
            return _dialogue_comprehension_repair_is_substantive(
                original_dialogue=original_dialogue,
                polished_dialogue=polished_dialogue,
                issues=issues,
                scene_metadata=scene_metadata,
            )
        return True

    if not _opener_changed_materially(original_dialogue, polished_dialogue):
        return False

    if "act:speaker_distribution_imbalance" in set(intervention_targets):
        dominant_opening = ""
        if isinstance(act_repetition_report, dict):
            dominant_opening = str(act_repetition_report.get("dominant_opening_speaker") or "").strip()
        if dominant_opening:
            original_speaker = _dialogue_edge_speaker(original_dialogue, edge="first")
            polished_speaker = _dialogue_edge_speaker(polished_dialogue, edge="first")
            if original_speaker == dominant_opening and polished_speaker == dominant_opening:
                return False

    return True


def _dialogue_line_tokens(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return re.findall(r"\b[\wА-Яа-яЁё'-]+\b", text.casefold())


def _dialogue_line_tokens_with_case(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return re.findall(r"\b[\wА-Яа-яЁё'-]+\b", text)


def _dialogue_text_ends_with_question(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return bool(stripped) and stripped.endswith("?")


def _dialogue_lexical_delta(original_text: Any, polished_text: Any) -> int:
    original_set = set(_dialogue_line_tokens(original_text))
    polished_set = set(_dialogue_line_tokens(polished_text))
    return len((original_set - polished_set) | (polished_set - original_set))


def _dialogue_line_pair_candidates(
    *,
    original_dialogue: Any,
    polished_dialogue: Any,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if not isinstance(original_dialogue, list) or not isinstance(polished_dialogue, list):
        return []
    candidate_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for original_line, polished_line in zip(original_dialogue, polished_dialogue):
        if not isinstance(original_line, dict) or not isinstance(polished_line, dict):
            continue
        if str(original_line.get("speaker") or "").strip() != str(polished_line.get("speaker") or "").strip():
            continue
        candidate_pairs.append((original_line, polished_line))
    return candidate_pairs


def _dialogue_generic_comprehension_delta_is_substantive(
    *,
    line_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    declarative_pairs = [
        (original_line, polished_line)
        for original_line, polished_line in line_pairs
        if not _dialogue_text_ends_with_question(original_line.get("text"))
        and not _dialogue_text_ends_with_question(polished_line.get("text"))
    ]
    pairs_to_check = declarative_pairs or line_pairs
    for original_line, polished_line in pairs_to_check:
        if _dialogue_lexical_delta(original_line.get("text"), polished_line.get("text")) >= 2:
            return True
    return False


def _candidate_new_terms_for_comprehension_check(
    *,
    original_dialogue: Any,
    scene_metadata: dict[str, Any] | None,
) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    if isinstance(scene_metadata, dict):
        for item in scene_metadata.get("new_terms_to_introduce", []) or []:
            if not isinstance(item, str):
                continue
            term = " ".join(item.strip().split())
            if not term:
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(term)
    if isinstance(original_dialogue, list):
        for line in original_dialogue:
            if not isinstance(line, dict):
                continue
            for token in _dialogue_line_tokens_with_case(line.get("text")):
                if len(token) < 2:
                    continue
                if not any(char.isalpha() for char in token):
                    continue
                if token.upper() != token:
                    continue
                key = token.casefold()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(token)
    return candidates


def _dialogue_text_contains_term(text: Any, term: str) -> bool:
    if not isinstance(text, str):
        return False
    return term.casefold() in text.casefold()


def _dialogue_term_repair_is_substantive(
    *,
    line_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    original_dialogue: Any,
    polished_dialogue: Any,
    scene_metadata: dict[str, Any] | None,
) -> bool:
    candidate_terms = _candidate_new_terms_for_comprehension_check(
        original_dialogue=original_dialogue,
        scene_metadata=scene_metadata,
    )
    if not candidate_terms:
        return _dialogue_generic_comprehension_delta_is_substantive(line_pairs=line_pairs)

    if not isinstance(polished_dialogue, list):
        return False

    for term in candidate_terms:
        original_has_term = any(
            isinstance(original_line, dict) and _dialogue_text_contains_term(original_line.get("text"), term)
            for original_line in (original_dialogue or [])
        )
        if not original_has_term:
            continue

        polished_has_term = any(
            isinstance(polished_line, dict) and _dialogue_text_contains_term(polished_line.get("text"), term)
            for polished_line in polished_dialogue
        )

        if not polished_has_term:
            if _dialogue_generic_comprehension_delta_is_substantive(line_pairs=line_pairs):
                return True
            continue

        for original_line, polished_line in line_pairs:
            original_text = original_line.get("text")
            polished_text = polished_line.get("text")
            if not _dialogue_text_contains_term(original_text, term):
                continue
            if not _dialogue_text_contains_term(polished_text, term):
                continue
            original_tokens = _dialogue_line_tokens(original_text)
            polished_tokens = _dialogue_line_tokens(polished_text)
            if not original_tokens or not polished_tokens:
                continue
            if len(polished_tokens) < len(original_tokens) + 3:
                continue
            if _dialogue_lexical_delta(original_text, polished_text) >= 3:
                return True

    return False


def _dialogue_comprehension_repair_is_substantive(
    *,
    original_dialogue: Any,
    polished_dialogue: Any,
    issues: list[dict[str, Any]],
    scene_metadata: dict[str, Any] | None,
) -> bool:
    line_pairs = _dialogue_line_pair_candidates(
        original_dialogue=original_dialogue,
        polished_dialogue=polished_dialogue,
    )
    if not line_pairs:
        return False

    validators_run = False
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_type = str(issue.get("type") or "").strip()
        if issue_type == "unexplained_term":
            validators_run = True
            if not _dialogue_term_repair_is_substantive(
                line_pairs=line_pairs,
                original_dialogue=original_dialogue,
                polished_dialogue=polished_dialogue,
                scene_metadata=scene_metadata,
            ):
                return False
            continue

    if validators_run:
        return True
    return _dialogue_generic_comprehension_delta_is_substantive(line_pairs=line_pairs)


def _dialogue_scene_metadata_prompt_excerpt(scene_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(scene_metadata, dict) or not scene_metadata:
        return None
    return {
        "scene_type": str(scene_metadata.get("scene_type") or "").strip(),
        "description": str(scene_metadata.get("description") or "").strip(),
        "listener_state_before": str(scene_metadata.get("listener_state_before") or "").strip(),
        "listener_state_after": str(scene_metadata.get("listener_state_after") or "").strip(),
        "must_make_clear": [
            str(item).strip()
            for item in scene_metadata.get("must_make_clear", [])
            if isinstance(item, str) and str(item).strip()
        ],
        "must_not_assume": [
            str(item).strip()
            for item in scene_metadata.get("must_not_assume", [])
            if isinstance(item, str) and str(item).strip()
        ],
        "new_terms_to_introduce": [
            str(item).strip()
            for item in scene_metadata.get("new_terms_to_introduce", [])
            if isinstance(item, str) and str(item).strip()
        ],
        "required_bridge_from_previous_scene": str(
            scene_metadata.get("required_bridge_from_previous_scene") or ""
        ).strip(),
    }


def _dialogue_word_count(dialogue: Any) -> int:
    if not isinstance(dialogue, list):
        return 0
    count = 0
    for line in dialogue:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        count += len(re.findall(r"\b[\wА-Яа-яЁё'-]+\b", text))
    return count


def _estimate_dialogue_timing(
    *,
    segments: list[ScriptSegment],
    scene_metadata_lookup: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    word_count = sum(_dialogue_word_count(segment.dialogue_json) for segment in segments)
    target_seconds = sum(
        int(metadata.get("estimated_duration_seconds") or 0)
        for metadata in scene_metadata_lookup.values()
        if isinstance(metadata, dict)
    )
    estimated_seconds_150 = round(word_count / 150 * 60)
    estimated_seconds_145 = round(word_count / 145 * 60)
    estimated_seconds_130 = round(word_count / 130 * 60)
    return {
        "word_count": word_count,
        "estimated_seconds_150_wpm": estimated_seconds_150,
        "estimated_seconds_145_wpm": estimated_seconds_145,
        "estimated_seconds_130_wpm": estimated_seconds_130,
        "target_seconds": target_seconds,
        "delta_seconds_vs_target_145_wpm": estimated_seconds_145 - target_seconds if target_seconds > 0 else None,
        "below_target_warning": estimated_seconds_145 < round(target_seconds * 0.9) if target_seconds > 0 else False,
    }


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


def _dump_dialogue_seam_debug(
    *,
    run_id: str,
    status: str,
    prompt_payload: dict[str, Any],
    audit: list[dict[str, Any]] | None,
    error: str | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="dialogue_seam_audit",
        filename_stem=f"run-{safe_filename_token(run_id)}_{safe_filename_token(status)}",
        payload={
            "run_id": run_id,
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


def _dump_multi_scene_polish_debug(
    *,
    run_id: str,
    candidate: dict[str, Any],
    status: str,
    payload: dict[str, Any],
    before_dialogues: dict[int, Any],
    after_dialogues: dict[int, Any] | None,
    decision: str,
    error: str | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    act_number = _safe_int(candidate.get("act_number"), 0)
    scene_numbers = "-".join(f"{scene_number:02d}" for scene_number in candidate.get("scene_numbers", []) if isinstance(scene_number, int))
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="dialogue_multi_scene_polish",
        filename_stem=(
            f"run-{safe_filename_token(run_id)}_"
            f"act-{act_number:02d}_scenes-{safe_filename_token(scene_numbers)}_{safe_filename_token(status)}"
        ),
        payload={
            "run_id": run_id,
            "act_number": act_number,
            "scene_numbers": candidate.get("scene_numbers"),
            "family": candidate.get("family"),
            "status": status,
            "decision": decision,
            "input": payload,
            "before_dialogues": before_dialogues,
            "after_dialogues": after_dialogues,
            "error": error,
        },
    )
