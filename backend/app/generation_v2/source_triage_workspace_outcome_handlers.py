from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS
from app.generation_v2.source_triage_workspace_handler_helpers import (
    confidence as _confidence,
    finding_lineage_errors as _finding_lineage_errors,
    finding_supports_question as _finding_supports_question,
    finding_supports_requirement as _finding_supports_requirement,
    lineage_repair_next_action as _lineage_repair_next_action,
    local_requirement_ids as _local_requirement_ids,
    outcome_argument_finding_ids as _outcome_argument_finding_ids,
    outcome_argument_lineage_errors as _outcome_argument_lineage_errors,
    outcome_argument_requirement_ids as _outcome_argument_requirement_ids,
    pending_set_work as _pending_set_work,
    requirement_status_consistency_error as _requirement_status_consistency_error,
)
from app.generation_v2.source_triage_workspace_state import invalidate_coverage_syntheses
from app.generation_v2.source_triage_model_delivery import (
    finding_catalog_delivery_state,
    question_workbench_is_fully_delivered,
    question_workbench_delivery_state,
)
from app.generation_v2.source_triage_question_state import finding_catalog_snapshot_id
from app.generation_v2.source_triage_set_state_views import (
    question_workbench_signature,
)
from app.generation_v2.source_triage_question_resolution import (
    apply_question_resolution_links,
    normalize_question_resolution_args,
    question_resolution_shape_error,
)
from app.generation_v2.source_triage_language_contract import (
    question_resolution_language_error,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref


class SourceTriageWorkspaceOutcomeHandlersMixin:
    async def upsert_coverage_outcome_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        question_id = str(args.get("question_id") or "").strip()
        question = next(
            (
                item
                for item in current.get("coverage_questions") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            None,
        )
        if question is None:
            return {
                "accepted": False,
                "error": "Unknown coverage question",
                "question_id": question_id,
                "next_action": "Copy an exact question_id from the persisted coverage question list.",
            }
        snapshot_id = str(args.get("workbench_snapshot_id") or "").strip()
        v3_resolution = bool(snapshot_id)
        if v3_resolution:
            current_snapshot_id = question_workbench_signature(
                current,
                source_set,
                question_id=question_id,
            )
            if snapshot_id != current_snapshot_id:
                catalog_snapshot_id = finding_catalog_snapshot_id(current, source_set)
                return {
                    "accepted": False,
                    "error": "question_workbench_snapshot_stale",
                    "question_id": question_id,
                    "current_workbench_snapshot_id": current_snapshot_id,
                    "current_finding_catalog_snapshot_id": catalog_snapshot_id,
                    "next_action": (
                        "The previous semantic decision is no longer valid. Read the current finding_catalog if "
                        "its snapshot changed, then read question_workbench from cursor='0' and recompute the "
                        "complete question result from the new state."
                    ),
                }
            catalog_snapshot_id = finding_catalog_snapshot_id(current, source_set)
            catalog_delivery = finding_catalog_delivery_state(
                current,
                set_id=set_id,
                snapshot_id=catalog_snapshot_id,
            )
            if not catalog_delivery["complete"]:
                return {
                    "accepted": False,
                    "error": "finding_catalog_not_fully_read",
                    "question_id": question_id,
                    "finding_catalog_snapshot_id": catalog_snapshot_id,
                    **catalog_delivery,
                    "next_action": (
                        "The attempted semantic decision is provisional. Read section=finding_catalog from "
                        f"cursor={catalog_delivery['first_unread_cursor']!r}, follow every page.next_cursor, then "
                        "reread the current question_workbench and recompute the result."
                    ),
                }
            if not question_workbench_is_fully_delivered(
                current,
                set_id=set_id,
                question_id=question_id,
                snapshot_id=snapshot_id,
            ):
                delivery = question_workbench_delivery_state(
                    current,
                    set_id=set_id,
                    question_id=question_id,
                    snapshot_id=snapshot_id,
                )
                return {
                    "accepted": False,
                    "error": "question_workbench_not_fully_read",
                    "question_id": question_id,
                    "workbench_snapshot_id": snapshot_id,
                    **delivery,
                    "next_action": (
                        "The attempted semantic decision is provisional, not a record to preserve. Read "
                        f"question_workbench from cursor={delivery['first_unread_cursor']!r}, follow every "
                        "page.next_cursor, then recompute every requirement affected by the newly read state."
                    ),
                }
            shape_error = question_resolution_shape_error(args)
            if shape_error:
                return shape_error
            args = normalize_question_resolution_args(args)
        language_error = question_resolution_language_error(
            args,
            output_language=str((current.get("intent") or {}).get("language") or ""),
        )
        if language_error:
            return language_error
        stored_findings_by_id = {
            str(item.get("finding_id") or ""): item
            for item in current.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("finding_id") or "")
        }
        findings_by_id = deepcopy(stored_findings_by_id) if v3_resolution else stored_findings_by_id
        if v3_resolution:
            link_error = apply_question_resolution_links(
                findings_by_id=findings_by_id,
                set_id=set_id,
                question=question,
                requirement_results=args.get("requirement_results") or [],
            )
            if link_error:
                return link_error
        current_question_ids = {str(item) for item in source_set.get("question_ids") or []}
        if question_id not in current_question_ids:
            linked_current_set_findings = [
                finding
                for finding in findings_by_id.values()
                if str(finding.get("set_id") or "") == set_id and _finding_supports_question(finding, question_id)
            ]
            if linked_current_set_findings:
                source_set["cross_question_ids"] = public_api._dedupe(
                    [*(source_set.get("cross_question_ids") or []), question_id]
                )
                source_set["question_ids"] = public_api._dedupe([*(source_set.get("question_ids") or []), question_id])
            else:
                attempted_finding_ids = _outcome_argument_finding_ids(args)
                current_set_finding_ids = [
                    finding_id
                    for finding_id in attempted_finding_ids
                    if finding_id in findings_by_id and str(findings_by_id[finding_id].get("set_id") or "") == set_id
                ]
                if current_set_finding_ids:
                    self._record_pending_discovered_question_attempt(
                        source_set=source_set,
                        question_id=question_id,
                        finding_ids=current_set_finding_ids,
                        findings_by_id=findings_by_id,
                        requirement_ids=_outcome_argument_requirement_ids(args),
                    )
                    public_api._replace_workspace_set(current, source_set)
                    await public_api.save_source_triage_workspace(
                        self.db,
                        generation_run_id=self.run.id,
                        workspace=current,
                    )
                    lineage_errors = _outcome_argument_lineage_errors(
                        args=args,
                        allowed_finding_ids=set(current_set_finding_ids),
                        findings_by_id=findings_by_id,
                        set_id=set_id,
                        question_id=question_id,
                    )
                    return {
                        "accepted": False,
                        "error": ("A discovered question becomes set work only through a persisted finding link"),
                        "question_id": question_id,
                        "lineage_errors": lineage_errors,
                        "next_action": (
                            "Resave each listed finding's owner source with a question_link to this question and "
                            "its exact requirement_ids, then retry the outcome. If the attempted link was wrong, "
                            "resave every listed owner source with its complete reviewed findings; an identical "
                            "accepted analysis is valid confirmation that no such link exists."
                        ),
                    }
                return {
                    "accepted": False,
                    "error": "question_id must belong to the current source set or have a linked current-set finding",
                    "question_id": question_id,
                    "next_action": (
                        "Use an assigned question, or first persist a source finding linked to this discovered question."
                    ),
                }
        if source_set.get("repair_targeted") and question_id not in {
            str(item) for item in source_set.get("repair_question_ids") or []
        }:
            return {
                "accepted": False,
                "error": "coverage outcome is outside the targeted repair scope",
                "question_id": question_id,
                "allowed_question_ids": list(source_set.get("repair_question_ids") or []),
            }
        status = str(args.get("status") or "unknown").strip()
        if status not in {"covered", "partial", "missing", "conflicted", "deferred"}:
            return {"accepted": False, "error": "coverage outcome status is not final", "status": status}
        requested_next_action = str(args.get("next_action") or "research").strip()
        if requested_next_action != "research":
            return {
                "accepted": False,
                "error": "per_set_outcome_cannot_route_run",
                "message": (
                    "A source-set outcome is local. Only the run-wide coverage synthesis may request "
                    "source acquisition, omission, or completion."
                ),
            }
        next_action = "research"

        finding_lineage_mode = "finding_ids" in args or "requirement_results" in args
        requirement_results: list[dict[str, Any]] = []
        finding_ids: list[str] = []
        if finding_lineage_mode:
            expected_requirement_ids = _local_requirement_ids(
                question=question or {"question_id": question_id, "answer_requirements": []},
                source_set=source_set,
                findings_by_id=findings_by_id,
                question_id=question_id,
            )
            requirement_results, result_finding_ids, requirement_error = self._validate_requirement_results(
                question=question or {"question_id": question_id, "answer_requirements": []},
                raw_results=args.get("requirement_results"),
                overall_status=status,
                findings_by_id=findings_by_id,
                set_id=set_id,
                allow_global_next_action=False,
                expected_requirement_ids=expected_requirement_ids,
            )
            if requirement_error:
                return {
                    **requirement_error,
                    "allowed_findings_by_requirement": _allowed_findings_by_requirement(
                        question=question,
                        findings_by_id=findings_by_id,
                        set_id=set_id,
                    ),
                    "next_action": (
                        "Read every page of read_triage_set_state section=question_workbench for this question_id, "
                        "then retry with its current snapshot and only findings that satisfy the exact criterion."
                        if v3_resolution
                        else
                        "Read read_triage_set_state section=question_memory for this question_id, then retry "
                        "using only findings shown under the intended requirement."
                    ),
                }
            provided_finding_ids = public_api._dedupe(
                [str(item) for item in args.get("finding_ids") or [] if str(item).strip()]
            )
            if (
                (question or {}).get("answer_requirements")
                and "finding_ids" in args
                and set(provided_finding_ids) != set(result_finding_ids)
            ):
                return {
                    "accepted": False,
                    "error": (
                        "For a structured question, outcome finding_ids must exactly equal the findings used "
                        "by its requirement results"
                    ),
                    "provided_finding_ids": provided_finding_ids,
                    "required_finding_ids": result_finding_ids,
                    "next_action": (
                        "Remove the legacy top-level finding_ids field, or make it match the union of "
                        "requirement_results[].finding_ids."
                    ),
                }
            finding_ids = public_api._dedupe(
                result_finding_ids
                if (question or {}).get("answer_requirements")
                else [*provided_finding_ids, *result_finding_ids]
            )
            unknown_finding_ids = sorted(set(finding_ids) - set(findings_by_id))
            wrong_set_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id and str(findings_by_id[finding_id].get("set_id") or "") != set_id
            )
            unlinked_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and not _finding_supports_question(findings_by_id[finding_id], question_id)
            )
            if unknown_finding_ids or wrong_set_finding_ids or unlinked_finding_ids:
                lineage_errors = _finding_lineage_errors(
                    finding_ids=finding_ids,
                    findings_by_id=findings_by_id,
                    set_id=set_id,
                    question_id=question_id,
                )
                return {
                    "accepted": False,
                    "error": "Outcome may use only current-set findings explicitly linked to this question",
                    "question_id": question_id,
                    "unknown_finding_ids": unknown_finding_ids,
                    "wrong_set_finding_ids": wrong_set_finding_ids,
                    "unlinked_finding_ids": unlinked_finding_ids,
                    "lineage_errors": lineage_errors,
                    "allowed_findings_by_requirement": _allowed_findings_by_requirement(
                        question=question,
                        findings_by_id=findings_by_id,
                        set_id=set_id,
                    ),
                    "next_action": (
                        f"{_lineage_repair_next_action(lineage_errors)} Then read section=question_memory before retrying."
                    ),
                }
            selected_findings = [findings_by_id[finding_id] for finding_id in finding_ids]
            source_refs = public_api._dedupe([str(item.get("source_ref") or "") for item in selected_findings])
            evidence_ids = public_api._dedupe(
                [str(evidence_id) for item in selected_findings for evidence_id in item.get("evidence_ids") or []]
            )
        else:
            # Compatibility for completed V2 work written before findings.
            source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
            evidence_ids = public_api._dedupe(
                [str(item) for item in args.get("evidence_ids") or [] if str(item).strip()]
            )
        invalid_refs = sorted(set(source_refs) - set(source_set.get("source_refs") or []))
        if invalid_refs:
            return {
                "accepted": False,
                "error": "coverage outcome source_refs must belong to the current source set",
                "invalid_source_refs": invalid_refs,
            }
        ref_context = build_source_ref_context(self.inventory)
        unresolved_refs = [ref for ref in source_refs if not resolve_source_ref(ref, ref_context)]
        if unresolved_refs:
            return {
                "accepted": False,
                "error": "coverage outcome source_refs could not be resolved in the current corpus view",
                "invalid_source_refs": unresolved_refs,
            }
        source_ids = [resolve_source_ref(ref, ref_context) for ref in source_refs]
        source_ids = [source_id for source_id in source_ids if source_id]
        selection_purpose, selection_role = {
            "covered": ("coverage_outcome", "triage_coverage_support"),
            "partial": ("coverage_outcome", "triage_coverage_support"),
            "conflicted": ("contradiction", "contradiction"),
            "missing": ("gap", "gap"),
            "deferred": ("gap", "gap"),
        }[status]
        evidence_error = await self._validate_set_evidence_ids(
            source_set=source_set,
            evidence_ids=evidence_ids,
            required_source_ids=set(source_ids),
            selection_purpose=selection_purpose,
            selection_role=selection_role,
        )
        if evidence_error:
            return evidence_error

        semantic_fields = {
            "coverage_summary": str(args.get("orientation_summary") or "").strip(),
            "missing_detail": str(args.get("missing_detail") or "").strip(),
            "conflict_summary": str(args.get("conflict_summary") or "").strip(),
            "deferred_reason": str(args.get("deferred_reason") or "").strip(),
        }
        if not v3_resolution:
            # The legacy tool authors these aggregate fields directly, so each
            # one must fit its declared input contract. The current resolution
            # tool authors bounded per-requirement fields instead; these values
            # are losslessly derived by joining them and must not acquire a
            # second, smaller aggregate limit.
            field_error = semantic_field_error(
                semantic_fields,
                {
                    "coverage_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_summary"],
                    "missing_detail": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "conflict_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "deferred_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                },
                list(semantic_fields),
            )
            if field_error:
                return field_error
        assessments_by_ref = {
            str(item.get("source_ref") or ""): item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        }
        has_representation_blocker = any(
            str((assessments_by_ref.get(str(source_ref)) or {}).get("representation_status") or "")
            in {"partial", "not_target_content", "unreadable"}
            for source_ref in source_set.get("source_refs") or []
        )
        deferred_requirement_ids = [
            str(result.get("requirement_id") or "")
            for result in requirement_results
            if str(result.get("status") or "") == "deferred"
        ]
        if deferred_requirement_ids and not has_representation_blocker:
            return {
                "accepted": False,
                "error": "A deferred requirement needs a checked representation blocker in this source set",
                "requirement_ids": deferred_requirement_ids,
                "next_action": (
                    "Continue reading and save the source analysis; if checked readable material is absent, "
                    "record the requirement as missing instead."
                ),
            }
        if status in {"covered", "partial", "conflicted"} and not source_ids:
            return {"accepted": False, "error": f"{status} outcome requires at least one source_ref"}
        if status in {"covered", "partial", "conflicted"} and not evidence_ids:
            return {"accepted": False, "error": f"{status} outcome requires saved evidence"}
        if status == "covered" and not semantic_fields["coverage_summary"]:
            return {"accepted": False, "error": "covered outcome requires orientation_summary"}
        if status == "partial" and not (semantic_fields["coverage_summary"] and semantic_fields["missing_detail"]):
            return {
                "accepted": False,
                "error": "partial outcome requires both orientation_summary and a concrete missing_detail",
            }
        if status == "missing" and not semantic_fields["missing_detail"]:
            return {"accepted": False, "error": "missing outcome requires missing_detail"}
        if status == "conflicted" and not semantic_fields["conflict_summary"]:
            return {"accepted": False, "error": "conflicted outcome requires conflict_summary"}
        if status == "deferred" and not semantic_fields["deferred_reason"]:
            return {"accepted": False, "error": "deferred outcome requires deferred_reason"}
        if status == "deferred":
            if not has_representation_blocker:
                return {
                    "accepted": False,
                    "error": (
                        "deferred outcome requires a checked partial/not_target_content/unreadable representation; "
                        "continue reading unfinished sources or use missing for checked absence"
                    ),
                    "next_action": (
                        "Save the remaining source analysis, then retry with deferred only for a real "
                        "representation blocker or missing after checked absence."
                    ),
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
                "error": "coverage outcome contains fields that contradict its final status",
                "status": status,
            }

        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose=selection_purpose,
            role=selection_role,
            rationale="Persisted by upsert_coverage_outcome.",
        )
        if selection_error:
            return selection_error

        outcome_id = f"outcome:{question_id}:{set_id}"
        outcome = {
            "outcome_id": outcome_id,
            "set_id": set_id,
            "question_id": question_id,
            "status": status,
            "source_refs": source_refs,
            "source_ids": source_ids,
            "evidence_ids": evidence_ids,
            "finding_ids": finding_ids,
            "requirement_results": requirement_results,
            "orientation_summary": semantic_fields["coverage_summary"],
            "missing_detail": semantic_fields["missing_detail"],
            "conflict_summary": semantic_fields["conflict_summary"],
            "deferred_reason": semantic_fields["deferred_reason"],
            "confidence": _confidence(args.get("confidence")),
            "next_action": next_action,
            "semantic_revision": int(current.get("triage_semantic_revision") or 0) + 1,
            "is_stale": False,
            "updated_at": public_api._now(),
        }
        if v3_resolution:
            outcome["workbench_snapshot_id"] = snapshot_id
        if v3_resolution:
            for finding_id, proposed in findings_by_id.items():
                stored = stored_findings_by_id.get(finding_id)
                if stored is not None:
                    stored["question_links"] = deepcopy(proposed.get("question_links") or [])
        current["coverage_outcomes"] = [
            item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and str(item.get("outcome_id") or "") != outcome_id
        ]
        current["coverage_outcomes"].append(outcome)
        source_set["pending_discovered_question_attempts"] = [
            item
            for item in source_set.get("pending_discovered_question_attempts") or []
            if not isinstance(item, dict) or str(item.get("question_id") or "") != question_id
        ]
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "outcome_id": outcome_id,
            "question_id": question_id,
            "status": status,
            **_pending_set_work(current, source_set),
        }

    @staticmethod
    def _validate_requirement_results(
        *,
        question: dict[str, Any],
        raw_results: Any,
        overall_status: str,
        findings_by_id: dict[str, dict[str, Any]],
        set_id: str | None,
        allow_global_next_action: bool,
        expected_requirement_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
        requirements = [item for item in question.get("answer_requirements") or [] if isinstance(item, dict)]
        known_ids = {
            str(item.get("requirement_id") or "") for item in requirements if str(item.get("requirement_id") or "")
        }
        expected_ids = known_ids if expected_requirement_ids is None else set(expected_requirement_ids)
        invalid_expected_ids = sorted(expected_ids - known_ids)
        if invalid_expected_ids:
            return (
                [],
                [],
                {
                    "accepted": False,
                    "error": "Source-set requirement assignment references unknown question requirements",
                    "unknown_requirement_ids": invalid_expected_ids,
                },
            )
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            return [], [], {"accepted": False, "error": "requirement_results must be an array"}
        if not expected_ids:
            if raw_results:
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Legacy question has no persisted answer requirements",
                        "next_action": "Use an empty requirement_results array for this legacy question.",
                    },
                )
            return [], [], None
        supplied_ids = [str(item.get("requirement_id") or "") for item in raw_results if isinstance(item, dict)]
        missing_ids = sorted(expected_ids - set(supplied_ids))
        unknown_ids = sorted(set(supplied_ids) - expected_ids)
        duplicate_ids = sorted(
            requirement_id for requirement_id in set(supplied_ids) if supplied_ids.count(requirement_id) > 1
        )
        if len(supplied_ids) != len(raw_results) or missing_ids or unknown_ids or duplicate_ids:
            return (
                [],
                [],
                {
                    "accepted": False,
                    "error": "Provide exactly one result for every requirement assigned to or found by this set",
                    "missing_requirement_ids": missing_ids,
                    "unknown_requirement_ids": unknown_ids,
                    "duplicate_requirement_ids": duplicate_ids,
                },
            )

        normalized: list[dict[str, Any]] = []
        all_finding_ids: list[str] = []
        result_statuses: list[str] = []
        for raw_result in raw_results:
            requirement_id = str(raw_result.get("requirement_id") or "")
            status = str(raw_result.get("status") or "").strip()
            if status not in {"covered", "partial", "missing", "conflicted", "deferred"}:
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "requirement result status is not final",
                        "requirement_id": requirement_id,
                        "status": status,
                    },
                )
            finding_ids = workspace_api._dedupe(
                [str(item) for item in raw_result.get("finding_ids") or [] if str(item).strip()]
            )
            unknown_finding_ids = sorted(set(finding_ids) - set(findings_by_id))
            wrong_set_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and set_id is not None
                and str(findings_by_id[finding_id].get("set_id") or "") != set_id
            )
            unlinked_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and not _finding_supports_requirement(
                    findings_by_id[finding_id],
                    str(question.get("question_id") or ""),
                    requirement_id,
                )
            )
            if unknown_finding_ids or wrong_set_finding_ids or unlinked_finding_ids:
                lineage_errors = _finding_lineage_errors(
                    finding_ids=finding_ids,
                    findings_by_id=findings_by_id,
                    set_id=set_id,
                    question_id=str(question.get("question_id") or ""),
                    requirement_id=requirement_id,
                )
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Requirement result may use only findings linked to this question requirement",
                        "question_id": str(question.get("question_id") or ""),
                        "requirement_id": requirement_id,
                        "unknown_finding_ids": unknown_finding_ids,
                        "wrong_set_finding_ids": wrong_set_finding_ids,
                        "unlinked_finding_ids": unlinked_finding_ids,
                        "lineage_errors": lineage_errors,
                        "next_action": _lineage_repair_next_action(lineage_errors),
                    },
                )
            summary = str(raw_result.get("summary") or "").strip()
            enhanced_completion_contract = (
                "closure_basis" in raw_result or "remaining_uncertainty" in raw_result
            )
            closure_basis = str(raw_result.get("closure_basis") or "").strip()
            remaining_uncertainty = str(raw_result.get("remaining_uncertainty") or "").strip()
            missing_detail = str(raw_result.get("missing_detail") or "").strip()
            conflict_summary = str(raw_result.get("conflict_summary") or "").strip()
            deferred_reason = str(raw_result.get("deferred_reason") or "").strip()
            field_error = semantic_field_error(
                {
                    "requirement_summary": summary,
                    "requirement_closure_basis": closure_basis,
                    "requirement_uncertainty": remaining_uncertainty,
                    "missing_detail": missing_detail,
                    "conflict_summary": conflict_summary,
                    "deferred_reason": deferred_reason,
                },
                {
                    "requirement_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_summary"],
                    "requirement_closure_basis": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_closure_basis"],
                    "requirement_uncertainty": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_uncertainty"],
                    "missing_detail": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "conflict_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "deferred_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                },
                [
                    "requirement_summary",
                    "requirement_closure_basis",
                    "requirement_uncertainty",
                    "missing_detail",
                    "conflict_summary",
                    "deferred_reason",
                ],
            )
            if field_error:
                return [], [], field_error
            if status in {"covered", "partial", "conflicted"} and not finding_ids:
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": f"{status} requirement result requires a linked finding",
                        "requirement_id": requirement_id,
                    },
                )
            if status == "covered" and (
                not summary
                or (enhanced_completion_contract and not closure_basis)
                or missing_detail
                or conflict_summary
                or deferred_reason
            ):
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": (
                            "covered requirement result needs summary, a completion-criterion closure_basis, "
                            "and no gap/conflict/deferred fields"
                        ),
                        "requirement_id": requirement_id,
                    },
                )
            if status == "partial" and (
                not summary
                or (enhanced_completion_contract and not closure_basis)
                or not missing_detail
                or conflict_summary
                or deferred_reason
            ):
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": (
                            "partial requirement result needs summary, the established closure_basis, "
                            "and a concrete missing_detail"
                        ),
                        "requirement_id": requirement_id,
                    },
                )
            if status == "missing" and (
                finding_ids or summary or not missing_detail or conflict_summary or deferred_reason
            ):
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "missing requirement result needs only a concrete missing_detail",
                        "requirement_id": requirement_id,
                    },
                )
            if status == "conflicted" and (not conflict_summary or deferred_reason):
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "conflicted requirement result needs linked findings and conflict_summary",
                        "requirement_id": requirement_id,
                    },
                )
            if status == "deferred" and (
                finding_ids or summary or missing_detail or conflict_summary or not deferred_reason
            ):
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "deferred requirement result needs only a representation-blocked reason",
                        "requirement_id": requirement_id,
                    },
                )
            next_action = str(raw_result.get("next_action") or "research").strip()
            if allow_global_next_action:
                if next_action not in {"research", "source_acquisition", "omit", "none"}:
                    return (
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "invalid requirement result next_action",
                            "requirement_id": requirement_id,
                        },
                    )
            elif next_action != "research":
                return (
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "A local requirement result cannot route run-wide work",
                        "requirement_id": requirement_id,
                    },
                )
            normalized.append(
                {
                    "requirement_id": requirement_id,
                    "status": status,
                    "finding_ids": finding_ids,
                    "summary": summary,
                    "closure_basis": closure_basis,
                    "remaining_uncertainty": remaining_uncertainty,
                    "missing_detail": missing_detail,
                    "conflict_summary": conflict_summary,
                    "deferred_reason": deferred_reason,
                    "confidence": _confidence(raw_result.get("confidence")),
                    "next_action": next_action,
                }
            )
            all_finding_ids.extend(finding_ids)
            result_statuses.append(status)

        consistency_error = _requirement_status_consistency_error(
            overall_status=overall_status,
            requirement_statuses=result_statuses,
        )
        if consistency_error:
            return [], [], consistency_error
        return normalized, workspace_api._dedupe(all_finding_ids), None


def _allowed_findings_by_requirement(
    *,
    question: dict[str, Any],
    findings_by_id: dict[str, dict[str, Any]],
    set_id: str,
) -> list[dict[str, Any]]:
    question_id = str(question.get("question_id") or "")
    result: list[dict[str, Any]] = []
    for requirement in question.get("answer_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("requirement_id") or "")
        finding_ids = sorted(
            finding_id
            for finding_id, finding in findings_by_id.items()
            if str(finding.get("set_id") or "") == set_id
            and _finding_supports_requirement(finding, question_id, requirement_id)
        )
        result.append(
            {
                "requirement_id": requirement_id,
                "finding_ids_preview": finding_ids[:12],
                "omitted_finding_count": max(0, len(finding_ids) - 12),
            }
        )
    return result


__all__ = ["SourceTriageWorkspaceOutcomeHandlersMixin"]
