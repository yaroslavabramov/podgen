from __future__ import annotations

from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_lineage import coverage_synthesis_lineage
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS
from app.generation_v2.source_triage_workspace_handler_helpers import (
    confidence as _confidence,
    question_next_action_from_requirements as _question_next_action_from_requirements,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref


class SourceTriageWorkspaceSynthesisHandlersMixin:
    async def upsert_coverage_synthesis_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Persist one question-level synthesis across all completed source sets."""

        from app.generation_v2.source_triage_canonical import (
            coverage_question_material_lineage,
            coverage_synthesis_input_hash,
            research_eligible,
        )

        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        question_id = str(args.get("question_id") or "").strip()
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        if question_id not in known_question_ids:
            return {"accepted": False, "error": "Unknown coverage question", "question_id": question_id}

        status = str(args.get("status") or "").strip()
        if status not in {"covered", "partial", "missing", "conflicted", "deferred"}:
            return {"accepted": False, "error": "coverage synthesis status is not final", "status": status}
        question = next(
            (
                item
                for item in current.get("coverage_questions") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            {"question_id": question_id, "answer_requirements": []},
        )
        findings_by_id = {
            str(item.get("finding_id") or ""): item
            for item in current.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("finding_id") or "")
        }
        requirement_results, finding_ids, requirement_error = self._validate_requirement_results(
            question=question,
            raw_results=args.get("requirement_results"),
            overall_status=status,
            findings_by_id=findings_by_id,
            set_id=None,
            allow_global_next_action=True,
            expected_requirement_ids=None,
        )
        if requirement_error:
            return requirement_error
        question_outcomes = [
            item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
        ]
        stale_outcome_refs = sorted(
            str(item.get("outcome_id") or "") for item in question_outcomes if bool(item.get("is_stale"))
        )
        if stale_outcome_refs:
            return {
                "accepted": False,
                "error": "Run-wide synthesis cannot use stale local outcomes",
                "stale_outcome_refs": stale_outcome_refs,
                "next_action": "Update the affected local outcomes from current findings first.",
            }
        outcome_statuses = {str(item.get("status") or "") for item in question_outcomes}
        if status == "missing" and outcome_statuses != {"missing"}:
            return {
                "accepted": False,
                "error": "missing_synthesis_requires_all_set_outcomes_missing",
                "outcome_statuses": sorted(outcome_statuses),
            }
        if status == "deferred" and (
            "deferred" not in outcome_statuses or not outcome_statuses.issubset({"missing", "deferred"})
        ):
            return {
                "accepted": False,
                "error": "deferred_synthesis_cannot_discard_active_set_outcomes",
                "outcome_statuses": sorted(outcome_statuses),
            }
        requested_next_action = str(args.get("next_action") or "research").strip()
        if requested_next_action not in {"research", "source_acquisition", "omit", "none"}:
            return {"accepted": False, "error": "invalid next_action"}
        next_action = _question_next_action_from_requirements(
            requirement_results=requirement_results,
            fallback=requested_next_action,
        )
        if next_action == "source_acquisition":
            if status == "covered":
                return {
                    "accepted": False,
                    "error": "covered_synthesis_cannot_request_source_acquisition",
                }
            assigned_sets = [
                item
                for item in current.get("source_sets") or []
                if isinstance(item, dict) and question_id in {str(value) for value in item.get("question_ids") or []}
            ]
            assigned_set_ids = {str(item.get("set_id") or "") for item in assigned_sets}
            outcome_set_ids = {
                str(item.get("set_id") or "")
                for item in current.get("coverage_outcomes") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            }
            unfinished_set_ids = sorted(
                str(item.get("set_id") or "")
                for item in assigned_sets
                if str(item.get("status") or "") not in {"mapped", "deferred"}
            )
            missing_outcome_set_ids = sorted(assigned_set_ids - outcome_set_ids)
            if not assigned_set_ids or unfinished_set_ids or missing_outcome_set_ids:
                return {
                    "accepted": False,
                    "error": "source_acquisition_requires_all_assigned_set_outcomes",
                    "unfinished_set_ids": unfinished_set_ids,
                    "missing_outcome_set_ids": missing_outcome_set_ids,
                }

        active_outcomes = [
            item for item in question_outcomes if str(item.get("status") or "") in {"covered", "partial", "conflicted"}
        ]
        source_ref_by_id = {
            str(item.get("source_id") or ""): str(item.get("source_ref") or "")
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and item.get("source_id") and item.get("source_ref")
        }
        derived_source_refs, derived_evidence_ids, outcome_refs = coverage_synthesis_lineage(
            question_outcomes,
            status=status,
            source_ref_by_id=source_ref_by_id,
        )
        if finding_ids:
            selected_findings = [findings_by_id[finding_id] for finding_id in finding_ids]
            derived_source_refs = public_api._dedupe([str(item.get("source_ref") or "") for item in selected_findings])
            derived_evidence_ids = public_api._dedupe(
                [str(evidence_id) for item in selected_findings for evidence_id in item.get("evidence_ids") or []]
            )
        manual_lineage = "source_refs" in args or "evidence_ids" in args
        source_refs = public_api._dedupe(
            [
                str(item)
                for item in (args.get("source_refs") if manual_lineage else derived_source_refs) or []
                if str(item).strip()
            ]
        )
        unknown_source_refs = sorted(set(source_refs) - {str(item) for item in current.get("source_order") or []})
        if unknown_source_refs:
            return {
                "accepted": False,
                "error": "coverage synthesis source_refs must belong to the current corpus view",
                "invalid_source_refs": unknown_source_refs,
            }
        ref_context = build_source_ref_context(self.inventory)
        source_ids = [resolve_source_ref(ref, ref_context) for ref in source_refs]
        unresolved_refs = [ref for ref, source_id in zip(source_refs, source_ids, strict=True) if not source_id]
        if unresolved_refs:
            return {
                "accepted": False,
                "error": "coverage synthesis source_refs could not be resolved in the current corpus view",
                "invalid_source_refs": unresolved_refs,
            }
        source_ids = [source_id for source_id in source_ids if source_id]

        material_lineage = coverage_question_material_lineage(current, question_id)
        active_outcome_source_refs = {
            *[str(source_ref) for outcome in active_outcomes for source_ref in outcome.get("source_refs") or []],
            *[
                source_ref_by_id.get(str(source_id), "")
                for outcome in active_outcomes
                for source_id in outcome.get("source_ids") or []
            ],
        } - {""}
        allowed_source_refs = (
            active_outcome_source_refs
            if status in {"covered", "partial", "conflicted"}
            else set(material_lineage["source_refs"])
        )
        unlinked_source_refs = sorted(set(source_refs) - allowed_source_refs)
        if unlinked_source_refs:
            return {
                "accepted": False,
                "error": "coverage synthesis sources must be attached to this question by an assessment, source set, or per-set outcome",
                "invalid_source_refs": unlinked_source_refs,
            }

        assessments_by_ref = {
            str(item.get("source_ref") or ""): item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
        }
        if status in {"covered", "partial", "conflicted"}:
            ineligible_refs = sorted(
                ref
                for ref in source_refs
                if ref not in assessments_by_ref or not research_eligible(assessments_by_ref[ref])
            )
            if ineligible_refs:
                return {
                    "accepted": False,
                    "error": "Active coverage synthesis cannot use deferred, unreadable, out-of-scope, or unusable sources",
                    "invalid_source_refs": ineligible_refs,
                }

        evidence_ids = public_api._dedupe(
            [
                str(item)
                for item in (args.get("evidence_ids") if manual_lineage else derived_evidence_ids) or []
                if str(item).strip()
            ]
        )
        active_outcome_evidence_ids = {
            str(evidence_id)
            for outcome in active_outcomes
            for evidence_id in outcome.get("evidence_ids") or []
            if str(evidence_id).strip()
        }
        allowed_evidence_ids = (
            active_outcome_evidence_ids
            if status in {"covered", "partial", "conflicted"}
            else set(material_lineage["evidence_ids"])
        )
        unreviewed_evidence_ids = sorted(set(evidence_ids) - allowed_evidence_ids)
        if unreviewed_evidence_ids:
            return {
                "accepted": False,
                "error": "coverage synthesis may use only evidence attached to this question by an assessment or per-set outcome",
                "invalid_evidence_ids": unreviewed_evidence_ids,
            }

        if status in {"covered", "partial", "conflicted"}:
            if self.evidence_toolset is None:
                return {
                    "accepted": False,
                    "error": "Evidence validation is unavailable; evidence-backed triage state was not persisted",
                }
            evidence_source_refs: set[str] = set()
            for evidence_id in evidence_ids:
                evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                if evidence is None:
                    continue
                evidence_source_ref = await self.evidence_toolset._source_ref_for_id(evidence.source_id)
                if evidence_source_ref:
                    evidence_source_refs.add(str(evidence_source_ref))
            unbacked_source_refs = sorted(set(source_refs) - evidence_source_refs)
            if unbacked_source_refs:
                return {
                    "accepted": False,
                    "error": "coverage_synthesis_source_refs_require_selected_evidence",
                    "invalid_source_refs": unbacked_source_refs,
                }

        semantic_fields = {
            "coverage_summary": str(args.get("orientation_summary") or "").strip(),
            "missing_detail": str(args.get("missing_detail") or "").strip(),
            "conflict_summary": str(args.get("conflict_summary") or "").strip(),
            "deferred_reason": str(args.get("deferred_reason") or "").strip(),
        }
        field_error = semantic_field_error(
            semantic_fields,
            {
                "coverage_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_summary"],
                "missing_detail": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                "conflict_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                "deferred_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
            },
            list(semantic_fields),
        )
        if field_error:
            return field_error

        if status in {"covered", "partial", "conflicted"} and not source_ids:
            return {"accepted": False, "error": f"{status} synthesis requires at least one source_ref"}
        if status in {"covered", "partial", "conflicted"} and not evidence_ids:
            return {"accepted": False, "error": f"{status} synthesis requires saved evidence"}
        if status == "covered" and not semantic_fields["coverage_summary"]:
            return {"accepted": False, "error": "covered synthesis requires orientation_summary"}
        if status == "partial" and not (semantic_fields["coverage_summary"] and semantic_fields["missing_detail"]):
            return {
                "accepted": False,
                "error": "partial synthesis requires both orientation_summary and a still-open missing_detail",
            }
        if status == "missing" and not semantic_fields["missing_detail"]:
            return {"accepted": False, "error": "missing synthesis requires missing_detail"}
        if status == "conflicted" and not semantic_fields["conflict_summary"]:
            return {"accepted": False, "error": "conflicted synthesis requires conflict_summary"}
        if status == "deferred" and not semantic_fields["deferred_reason"]:
            return {"accepted": False, "error": "deferred synthesis requires deferred_reason"}
        if status == "deferred" and not any(
            isinstance(item, dict)
            and str(item.get("question_id") or "") == question_id
            and str(item.get("status") or "") == "deferred"
            for item in current.get("coverage_outcomes") or []
        ):
            return {
                "accepted": False,
                "error": "deferred synthesis requires at least one representation-blocked per-set outcome",
            }
        contradictory_fields = {
            "covered": [semantic_fields["missing_detail"], semantic_fields["deferred_reason"]],
            "partial": [semantic_fields["deferred_reason"]],
            "missing": [
                semantic_fields["coverage_summary"],
                semantic_fields["conflict_summary"],
                semantic_fields["deferred_reason"],
            ],
            "conflicted": [semantic_fields["deferred_reason"]],
            "deferred": [
                semantic_fields["coverage_summary"],
                semantic_fields["missing_detail"],
                semantic_fields["conflict_summary"],
            ],
        }[status]
        if any(contradictory_fields):
            return {
                "accepted": False,
                "error": "coverage synthesis contains fields that contradict its final status",
                "status": status,
            }

        selection_purpose, selection_role = {
            "covered": ("coverage_outcome", "triage_coverage_synthesis"),
            "partial": ("coverage_outcome", "triage_coverage_synthesis"),
            "conflicted": ("contradiction", "triage_coverage_synthesis"),
            "missing": ("gap", "gap"),
            "deferred": ("gap", "gap"),
        }[status]
        evidence_error = await self._validate_set_evidence_ids(
            source_set={"source_refs": list(current.get("source_order") or [])},
            evidence_ids=evidence_ids,
            required_source_ids=set(source_ids),
            selection_purpose=selection_purpose,
            selection_role=selection_role,
        )
        if evidence_error:
            return evidence_error
        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose=selection_purpose,
            role=selection_role,
            rationale="Persisted by upsert_coverage_synthesis.",
        )
        if selection_error:
            return selection_error

        synthesis_id = f"synthesis:{question_id}"
        synthesis = {
            "outcome_id": synthesis_id,
            "question_id": question_id,
            "status": status,
            "source_refs": source_refs,
            "source_ids": source_ids,
            "evidence_ids": evidence_ids,
            "outcome_refs": outcome_refs,
            "finding_ids": finding_ids,
            "requirement_results": requirement_results,
            "orientation_summary": semantic_fields["coverage_summary"],
            "missing_detail": semantic_fields["missing_detail"],
            "conflict_summary": semantic_fields["conflict_summary"],
            "deferred_reason": semantic_fields["deferred_reason"],
            "confidence": _confidence(args.get("confidence")),
            "next_action": next_action,
            "input_revision": int(current.get("triage_semantic_revision") or 0),
            "input_hash": coverage_synthesis_input_hash(current, question_id),
            "updated_at": public_api._now(),
        }
        current["coverage_syntheses"] = [
            item
            for item in current.get("coverage_syntheses") or []
            if not isinstance(item, dict) or str(item.get("question_id") or "") != question_id
        ]
        current["coverage_syntheses"].append(synthesis)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "synthesis_ref": synthesis_id,
            "question_id": question_id,
            "status": status,
            "lineage_derived_from_outcomes": not manual_lineage,
            "source_ref_count": len(source_refs),
            "evidence_count": len(evidence_ids),
        }


__all__ = ["SourceTriageWorkspaceSynthesisHandlersMixin"]
