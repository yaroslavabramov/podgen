from __future__ import annotations

from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS
from app.generation_v2.source_triage_workspace_state import (
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
    invalidate_coverage_syntheses,
    uncheckpointed_source_read_replays,
)
from app.generation_v2.source_triage_read_checkpointing import (
    pending_comparison_source_refs,
    pending_exact_read_source_refs,
    source_refs_with_reads_newer_than_analysis,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref


class SourceTriageWorkspaceCompletionHandlersMixin:
    async def finish_triage_source_set_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        set_kind = str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE)
        if set_kind not in {SOURCE_SET_KIND_COVERAGE, SOURCE_SET_KIND_ORIENTATION}:
            return {"accepted": False, "error": "Unknown source set kind", "set_kind": set_kind}
        planned_question_ids = [
            str(item) for item in source_set.get("planned_question_ids", source_set.get("question_ids") or [])
        ]
        if set_kind == SOURCE_SET_KIND_COVERAGE and not planned_question_ids and not source_set.get("repair"):
            return {
                "accepted": False,
                "error": "A coverage source set requires at least one planned coverage question",
            }
        if set_kind == SOURCE_SET_KIND_ORIENTATION and planned_question_ids:
            return {
                "accepted": False,
                "error": "An initial-reading source set cannot have planner-assigned coverage questions",
                "question_ids": planned_question_ids,
            }
        status = str(args.get("status") or "").strip()
        if status not in {"mapped", "deferred"}:
            return {
                "accepted": False,
                "error": "status must be mapped or deferred",
                "next_action": "Persist the unfinished source analyses and outcomes, then retry this terminal tool.",
            }
        uncheckpointed_reads = uncheckpointed_source_read_replays(current, set_id=set_id)
        pending_exact_refs = pending_exact_read_source_refs(current, source_set=source_set)
        pending_comparison_refs = pending_comparison_source_refs(source_set)
        stale_analysis_refs = source_refs_with_reads_newer_than_analysis(current, set_id=set_id)
        if uncheckpointed_reads or pending_exact_refs or pending_comparison_refs or stale_analysis_refs:
            return {
                "accepted": False,
                "error": "source_reading_not_reconciled",
                "uncheckpointed_receipt_refs": [
                    str(item.get("receipt_ref") or "") for item in uncheckpointed_reads
                ],
                "pending_exact_source_refs": pending_exact_refs,
                "pending_comparison_source_refs": pending_comparison_refs,
                "stale_source_analysis_refs": stale_analysis_refs,
                "next_action": (
                    "Save durable reading notes where reading is still in progress, and resave the complete source "
                    "analysis for every source with newer exact or comparison material. Then refresh stale outcomes."
                ),
            }
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"accepted": False, "error": "summary is required"}
        ref_context = build_source_ref_context(self.inventory)
        source_ids_by_ref = {
            str(ref): resolve_source_ref(str(ref), ref_context) for ref in source_set.get("source_refs") or []
        }
        assessments_by_source = {
            str(item.get("source_id") or ""): item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and item.get("set_id") == set_id
        }
        assessments_by_ref = {
            ref: assessments_by_source[source_id]
            for ref, source_id in source_ids_by_ref.items()
            if source_id and source_id in assessments_by_source
        }
        missing_assessment_refs = [
            ref
            for ref, source_id in source_ids_by_ref.items()
            if not source_id or source_id not in assessments_by_source
        ]
        if missing_assessment_refs:
            return {
                "accepted": False,
                "error": "Every source in the set needs a persisted source assessment",
                "missing_source_refs": missing_assessment_refs,
            }
        missing_analysis_refs = sorted(
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if not assessment.get("finding_ids") and not str(assessment.get("no_findings_reason") or "").strip()
        )
        if missing_analysis_refs:
            return {
                "accepted": False,
                "error": "Every source needs durable findings or an explicit no_findings_reason",
                "missing_source_refs": missing_analysis_refs,
                "next_action": "Read each source and call save_source_analysis.",
            }
        unfinished_assessment_refs = sorted(
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if assessment.get("processing_status") is not None
            and str(assessment.get("processing_status")) != "resolved"
        )
        if unfinished_assessment_refs:
            return {
                "accepted": False,
                "error": "mapped/deferred set requires completed source analyses",
                "source_refs": unfinished_assessment_refs,
                "next_action": "Complete and persist these source analyses, then retry finish_triage_source_set.",
            }
        pending_discovered_attempts = [
            item
            for item in source_set.get("pending_discovered_question_attempts") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        ]
        if pending_discovered_attempts:
            return {
                "accepted": False,
                "error": "Rejected discovered-question lineage attempts still need a durable correction",
                "pending_question_attempts": [
                    {
                        "question_id": str(item.get("question_id") or ""),
                        "requirement_ids": [str(value) for value in item.get("requirement_ids") or []],
                        "finding_ids": [str(value) for value in item.get("finding_ids") or []],
                        "source_refs": [str(value) for value in item.get("source_refs") or []],
                        "unreviewed_source_refs": sorted(
                            {str(value) for value in item.get("source_refs") or []}
                            - {str(value) for value in item.get("reviewed_source_refs") or []}
                        ),
                    }
                    for item in pending_discovered_attempts
                ],
                "next_action": (
                    "For each pending question, resave the listed owner source with the intended exact "
                    "question/requirement links and then save its outcome. If the attempted link was wrong, "
                    "resave every listed owner source with its complete reviewed analysis; an identical accepted "
                    "analysis is valid confirmation that no such link exists."
                ),
            }
        outcomes_by_question = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and item.get("set_id") == set_id
        }
        missing_outcome_ids = [
            str(question_id)
            for question_id in source_set.get("question_ids") or []
            if str(question_id) not in outcomes_by_question
        ]
        if missing_outcome_ids:
            return {
                "accepted": False,
                "error": "Every assigned coverage question needs a final outcome",
                "missing_question_ids": missing_outcome_ids,
            }
        stale_outcome_ids = sorted(
            question_id for question_id, outcome in outcomes_by_question.items() if bool(outcome.get("is_stale"))
        )
        if stale_outcome_ids:
            return {
                "accepted": False,
                "error": "Every question outcome must be refreshed after later linked findings",
                "stale_question_ids": stale_outcome_ids,
                "next_action": "Update these outcomes from the current saved findings.",
            }
        has_representation_blocker = any(
            str(assessment.get("representation_status") or "") in {"partial", "not_target_content", "unreadable"}
            for assessment in assessments_by_ref.values()
        )
        unjustified_deferred_question_ids = sorted(
            question_id
            for question_id, outcome in outcomes_by_question.items()
            if (
                str(outcome.get("status") or "") == "deferred"
                or any(
                    isinstance(result, dict) and str(result.get("status") or "") == "deferred"
                    for result in outcome.get("requirement_results") or []
                )
            )
            and not has_representation_blocker
        )
        if unjustified_deferred_question_ids:
            return {
                "accepted": False,
                "error": "Deferred outcomes no longer have a checked representation blocker",
                "question_ids": unjustified_deferred_question_ids,
                "next_action": (
                    "Refresh these outcomes from the current source analyses, using missing only for checked absence."
                ),
            }
        if status == "mapped":
            has_semantic_state = bool(
                source_set.get("observations")
                or source_set.get("written_actions")
                or source_set.get("evidence_ids")
                or assessments_by_source
                or outcomes_by_question
            )
            if not has_semantic_state:
                return {
                    "accepted": False,
                    "error": "mapped set requires at least one observation, write action, or evidence_id",
                }
        if status == "deferred" and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred status"}
        deferred_reason = str(args.get("deferred_reason") or "").strip()
        if status == "mapped" and deferred_reason:
            return {
                "accepted": False,
                "error": "mapped set must keep deferred_reason empty; use deferred_sources for individual sources",
            }
        deferred_sources = []
        for item in args.get("deferred_sources") or []:
            if not isinstance(item, dict):
                continue
            source_ref = str(item.get("source_ref") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if source_ref not in set(source_set.get("source_refs") or []):
                return {
                    "accepted": False,
                    "error": "deferred source_ref must belong to the current source set",
                    "source_ref": source_ref,
                }
            if not reason:
                return {"accepted": False, "error": "Each deferred source requires a reason", "source_ref": source_ref}
            reason_error = semantic_field_error(
                {"deferred_reason": reason}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["deferred_reason"]
            )
            if reason_error:
                return reason_error
            deferred_sources.append({"source_ref": source_ref, "reason": reason})
        requested_deferred_reasons = {item["source_ref"]: item["reason"] for item in deferred_sources}
        assessment_deferred_refs = {
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if str(assessment.get("disposition") or "") == "deferred"
        }
        contradictory_deferred_refs = sorted(set(requested_deferred_reasons) - assessment_deferred_refs)
        if contradictory_deferred_refs:
            return {
                "accepted": False,
                "error": "A deferred source must have disposition=deferred in its persisted assessment",
                "source_refs": contradictory_deferred_refs,
            }
        if status == "deferred":
            nondeferred_assessment_refs = sorted(set(source_ids_by_ref) - assessment_deferred_refs)
            if nondeferred_assessment_refs:
                return {
                    "accepted": False,
                    "error": "A deferred set requires disposition=deferred for every source assessment",
                    "source_refs": nondeferred_assessment_refs,
                }
            active_outcome_question_ids = sorted(
                question_id
                for question_id, outcome in outcomes_by_question.items()
                if str(outcome.get("status") or "") != "deferred"
            )
            if active_outcome_question_ids:
                return {
                    "accepted": False,
                    "error": "A deferred set requires deferred outcomes for all assigned questions",
                    "question_ids": active_outcome_question_ids,
                }

        ineligible_assessment_refs = {
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if str(assessment.get("representation_status") or "unchecked") not in {"readable", "partial"}
            or str(assessment.get("relevance") or "unknown") == "out_of_scope"
            or str(assessment.get("disposition") or "background") in {"unusable", "deferred"}
        }
        active_outcomes_using_ineligible: list[str] = []
        for question_id, outcome in outcomes_by_question.items():
            if str(outcome.get("status") or "") not in {"covered", "partial", "conflicted"}:
                continue
            outcome_refs = {str(item) for item in outcome.get("source_refs") or []}
            outcome_source_ids = {str(item) for item in outcome.get("source_ids") or []}
            outcome_refs.update(ref for ref, source_id in source_ids_by_ref.items() if source_id in outcome_source_ids)
            if outcome_refs.intersection(ineligible_assessment_refs):
                active_outcomes_using_ineligible.append(question_id)
        if active_outcomes_using_ineligible:
            return {
                "accepted": False,
                "error": (
                    "Covered, partial, or conflicted outcomes cannot use not-target-content, unreadable, "
                    "out-of-scope, unusable, or deferred sources"
                ),
                "question_ids": sorted(active_outcomes_using_ineligible),
            }
        if status == "mapped":
            assessment_refs = set(assessments_by_ref) - assessment_deferred_refs
            covered_refs = {
                *public_api._source_set_covered_refs(source_set),
                *assessment_refs,
            } - assessment_deferred_refs
            unresolved_refs = [
                str(ref)
                for ref in source_set.get("source_refs") or []
                if str(ref) not in covered_refs and str(ref) not in assessment_deferred_refs
            ]
            if unresolved_refs:
                return {
                    "accepted": False,
                    "error": "Every source in a mapped set must be covered by a durable observation/write or explicitly deferred",
                    "unresolved_source_refs": unresolved_refs,
                }
            if not covered_refs:
                return {"accepted": False, "error": "Use deferred status when no source in the set was mapped"}
        field_error = semantic_field_error(
            {"findings_summary": summary, "deferred_reason": deferred_reason},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["findings_summary", "deferred_reason"],
        )
        if field_error:
            return field_error
        source_set["status"] = status
        source_set["findings_summary"] = summary
        source_set["deferred_reason"] = deferred_reason
        source_set["assessment_source_refs"] = [ref for ref in source_ids_by_ref if ref not in assessment_deferred_refs]
        source_set["deferred_source_refs"] = [ref for ref in source_ids_by_ref if ref in assessment_deferred_refs]
        source_set["outcome_question_ids"] = list(outcomes_by_question)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        processed = [item for item in current.get("processed_sets") or [] if item.get("set_id") != set_id]
        processed.append(
            {
                "set_id": set_id,
                "status": status,
                "summary": source_set["findings_summary"],
                "updated_at": public_api._now(),
            }
        )
        current["processed_sets"] = processed
        current["current_set"] = None
        if status in {"mapped", "deferred"}:
            for field in (
                "repair_baseline_source_assessments",
                "repair_baseline_coverage_outcomes",
            ):
                if field in current:
                    current[field] = [
                        item
                        for item in current.get(field) or []
                        if not isinstance(item, dict) or str(item.get("set_id") or "") != set_id
                    ]
        deferral_reason_overrides = dict(requested_deferred_reasons)
        if status == "deferred":
            deferral_reason_overrides.update(
                {
                    str(ref): deferred_reason or summary
                    for ref in source_set.get("source_refs") or []
                    if str(ref) not in deferral_reason_overrides
                }
            )
        self._sync_source_resolution_indexes(current, reason_overrides=deferral_reason_overrides)
        invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "status": status, "processed_set_count": len(processed)}
