from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_EDITORIAL_FUNCTION_LIMIT,
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_PENDING_QUESTION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_lineage import coverage_synthesis_lineage
from app.generation_v2.source_triage_workspace_planning_handlers import SourceTriageWorkspacePlanningHandlersMixin
from app.generation_v2.source_triage_workspace_state import (
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
    invalidate_coverage_syntheses,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.generation_v2.tools.evidence_utils import _unsupported_model_read_selected_as_support
from app.ingestion.web_quality import assess_web_fetch_health
from app.models.generation_run import GenerationRun


def _pending_set_work(workspace: dict[str, Any], source_set: dict[str, Any]) -> dict[str, list[str]]:
    set_id = str(source_set.get("set_id") or "")
    assessed_refs = {
        str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "resolved") == "resolved"
    }
    completed_question_ids = {
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("status") or "") in {"covered", "partial", "missing", "conflicted", "deferred"}
    }
    required_question_ids = [str(item) for item in source_set.get("question_ids") or []]
    return {
        "pending_source_refs": [
            str(item) for item in source_set.get("source_refs") or [] if str(item) not in assessed_refs
        ],
        "required_question_ids": required_question_ids,
        "missing_outcome_question_ids": [
            question_id for question_id in required_question_ids if question_id not in completed_question_ids
        ],
    }


@dataclass
class SourceTriageWorkspaceToolHandlers(SourceTriageWorkspacePlanningHandlersMixin):
    db: AsyncSession
    run: GenerationRun
    workspace: dict[str, Any]
    inventory: SourceMap
    evidence_toolset: Any | None = None
    current_set_id: str | None = None

    async def upsert_source_assessment_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        source_ref = str(args.get("source_ref") or "").strip()
        if source_ref not in set(source_set.get("source_refs") or []):
            return {
                "accepted": False,
                "error": "source_ref must belong to the current source set",
                "source_ref": source_ref,
            }
        if source_set.get("repair_targeted") and source_ref not in {
            str(item) for item in source_set.get("repair_source_refs") or []
        }:
            return {
                "accepted": False,
                "error": "source assessment is outside the targeted repair scope",
                "source_ref": source_ref,
                "allowed_source_refs": list(source_set.get("repair_source_refs") or []),
            }
        source_id = resolve_source_ref(source_ref, build_source_ref_context(self.inventory))
        if not source_id:
            return {"accepted": False, "error": "Unknown source_ref", "source_ref": source_ref}

        enum_fields = {
            "processing_status": {"pending", "in_progress", "resolved", "needs_repair"},
            "representation_status": {
                "unchecked",
                "readable",
                "partial",
                "not_target_content",
                "unreadable",
            },
            "relevance": {"core", "supporting", "contextual", "out_of_scope", "unknown"},
            "authority": {"primary", "official", "expert", "secondary", "tertiary", "weak", "unknown"},
            "uniqueness": {"unique", "complementary", "derivative", "duplicate", "unknown"},
            "disposition": {"anchor", "supporting", "background", "weak", "unusable", "deferred"},
        }
        values = {
            "processing_status": str(args.get("processing_status") or "resolved"),
            "representation_status": str(args.get("representation_status") or "unchecked"),
            "relevance": str(args.get("relevance") or "unknown"),
            "authority": str(args.get("authority") or "unknown"),
            "uniqueness": str(args.get("uniqueness") or "unknown"),
            "disposition": str(args.get("disposition") or "background"),
        }
        for field, allowed in enum_fields.items():
            if values[field] not in allowed:
                return {"accepted": False, "error": f"invalid {field}", "value": values[field]}
        if values["processing_status"] in {"pending", "in_progress"}:
            return {
                "accepted": False,
                "error": "A persisted source assessment must be resolved or explicitly needs_repair",
            }
        if values["processing_status"] == "resolved" and values["representation_status"] == "unchecked":
            return {
                "accepted": False,
                "error": "A resolved source assessment must check the available representation",
            }

        reason_fields = {
            "relevance_reason": str(args.get("relevance_reason") or "").strip(),
            "authority_reason": str(args.get("authority_reason") or "").strip(),
            "uniqueness_reason": str(args.get("uniqueness_reason") or "").strip(),
            "representation_reason": str(args.get("representation_reason") or "").strip(),
            "disposition_reason": str(args.get("disposition_reason") or "").strip(),
        }
        reason_error = semantic_field_error(
            {key: value for key, value in reason_fields.items()},
            {
                "relevance_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "authority_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "uniqueness_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "representation_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "disposition_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
            },
            list(reason_fields),
        )
        if reason_error:
            return reason_error
        if values["representation_status"] in {
            "partial",
            "not_target_content",
            "unreadable",
        } and not reason_fields["representation_reason"]:
            return {
                "accepted": False,
                "error": (
                    "partial/not_target_content/unreadable representation requires representation_reason"
                ),
            }
        if values["disposition"] in {"weak", "unusable", "deferred"} and not reason_fields[
            "disposition_reason"
        ]:
            return {
                "accepted": False,
                "error": "weak/unusable/deferred disposition requires disposition_reason",
            }
        if values["representation_status"] in {
            "not_target_content",
            "unreadable",
        } and values["disposition"] != "deferred":
            return {
                "accepted": False,
                "error": (
                    "A not_target_content or unreadable representation must use disposition=deferred; "
                    "it does not prove source value"
                ),
            }
        if values["disposition"] == "deferred" and values["representation_status"] not in {
            "partial",
            "not_target_content",
            "unreadable",
        }:
            return {
                "accepted": False,
                "error": (
                    "disposition=deferred requires a checked partial, not_target_content, or unreadable "
                    "representation"
                ),
            }
        if values["representation_status"] == "not_target_content":
            if values["processing_status"] != "resolved":
                return {
                    "accepted": False,
                    "error": (
                        "not_target_content is a completed representation diagnosis and requires "
                        "processing_status=resolved"
                    ),
                }
            non_unknown_axes = [
                field
                for field in ("relevance", "authority", "uniqueness")
                if values[field] != "unknown"
            ]
            if non_unknown_axes:
                return {
                    "accepted": False,
                    "error": (
                        "not_target_content cannot establish relevance, authority, or uniqueness; "
                        "all three judgments must be unknown"
                    ),
                    "invalid_fields": non_unknown_axes,
                }
            asserted_reason_fields = [
                field
                for field in ("relevance_reason", "authority_reason", "uniqueness_reason")
                if reason_fields[field]
            ]
            if asserted_reason_fields:
                return {
                    "accepted": False,
                    "error": (
                        "not_target_content source judgments are unknown; their semantic reason fields must be empty"
                    ),
                    "invalid_fields": asserted_reason_fields,
                }
        if values["disposition"] in {"anchor", "supporting"} and (
            values["representation_status"] in {"not_target_content", "unreadable"}
            or values["relevance"] == "out_of_scope"
        ):
            return {
                "accepted": False,
                "error": (
                    "A not-target-content, unreadable, or out-of-scope source cannot be an active research input"
                ),
            }

        question_ids = public_api._dedupe(
            [str(item) for item in args.get("coverage_question_ids") or [] if str(item).strip()]
        )
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "").strip()
        }
        unknown_question_ids = sorted(set(question_ids) - known_question_ids)
        if unknown_question_ids:
            return {
                "accepted": False,
                "error": "coverage_question_ids must be persisted questions in the current run",
                "unknown_question_ids": unknown_question_ids,
            }
        if source_set.get("repair_targeted"):
            out_of_scope_question_ids = sorted(
                set(question_ids) - {str(item) for item in source_set.get("question_ids") or []}
            )
            if out_of_scope_question_ids:
                return {
                    "accepted": False,
                    "error": "assessment question links are outside the targeted repair scope",
                    "question_ids": out_of_scope_question_ids,
                }
        editorial_functions = public_api._dedupe(
            [str(item) for item in args.get("editorial_functions") or [] if str(item).strip()]
        )
        list_error = semantic_list_error(
            field="editorial_functions",
            values=editorial_functions,
            max_items=TRIAGE_EDITORIAL_FUNCTION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["question"],
        )
        if list_error:
            return list_error
        if values["representation_status"] == "not_target_content" and (
            question_ids or editorial_functions
        ):
            return {
                "accepted": False,
                "error": (
                    "not_target_content cannot establish coverage-question or editorial-function links"
                ),
                "coverage_question_ids": question_ids,
                "editorial_functions": editorial_functions,
            }
        limitations = [str(item).strip() for item in args.get("limitations") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="limitations",
            values=limitations,
            max_items=TRIAGE_LIMITATION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
        )
        if list_error:
            return list_error
        evidence_ids = public_api._dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        requires_grounded_evidence = (
            values["representation_status"] == "not_target_content"
            or (
                values["processing_status"] == "resolved"
                and values["representation_status"] in {"readable", "partial"}
            )
        )
        if requires_grounded_evidence and not evidence_ids:
            return {
                "accepted": False,
                "error": (
                    "A resolved readable/partial/not_target_content source assessment requires grounded evidence "
                    "from that source"
                ),
            }
        supports_content_judgment = (
            values["processing_status"] == "resolved"
            and values["representation_status"] in {"readable", "partial"}
        )
        selection_purpose = "source_assessment" if supports_content_judgment else "caution"
        selection_role = "triage_source_assessment" if supports_content_judgment else "caution"
        evidence_error = await self._validate_set_evidence_ids(
            source_set=source_set,
            evidence_ids=evidence_ids,
            required_source_id=source_id,
            selection_purpose=selection_purpose,
            selection_role=selection_role,
        )
        if evidence_error:
            return evidence_error
        if supports_content_judgment:
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(evidence_ids)
            if evidence_ids and len(blocked_evidence_ids) == len(evidence_ids):
                return {
                    "accepted": False,
                    "error": (
                        "Shell/error/access-page evidence cannot establish readable or partial target content; "
                        "use representation_status=not_target_content with unknown judgments and "
                        "disposition=deferred"
                    ),
                    "invalid_evidence_ids": blocked_evidence_ids,
                    "required_representation_status": "not_target_content",
                }

        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose=selection_purpose,
            role=selection_role,
            rationale="Persisted by upsert_source_assessment.",
        )
        if selection_error:
            return selection_error

        inventory_card = next(
            (card for card in self.inventory.source_cards if str(card.source_id) == source_id),
            None,
        )
        assessment = {
            "assessment_id": f"assessment:{source_id}",
            "set_id": set_id,
            "source_ref": source_ref,
            "source_id": source_id,
            "source_version_ids": list(getattr(inventory_card, "source_version_ids", []) or []),
            "source_profile_id": getattr(inventory_card, "source_profile_id", None),
            **values,
            "representation_reason": reason_fields["representation_reason"],
            "relevance_reason": reason_fields["relevance_reason"],
            "authority_reason": reason_fields["authority_reason"],
            "uniqueness_reason": reason_fields["uniqueness_reason"],
            "disposition_reason": reason_fields["disposition_reason"],
            "coverage_question_ids": question_ids,
            "editorial_functions": editorial_functions,
            "evidence_ids": evidence_ids,
            "limitations": limitations,
            "confidence": _confidence(args.get("confidence")),
            "updated_at": public_api._now(),
        }
        current["source_assessments"] = [
            item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_id") or "") != source_id
        ]
        current["source_assessments"].append(assessment)
        self._recompute_source_set_question_links(current, source_set)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "source_ref": source_ref,
            "processing_status": values["processing_status"],
            "representation_status": values["representation_status"],
            "disposition": values["disposition"],
            **_pending_set_work(current, source_set),
        }

    async def _blocked_shell_evidence_ids(self, evidence_ids: list[str]) -> list[str]:
        if not evidence_ids or self.evidence_toolset is None:
            return []
        blocked: list[str] = []
        for evidence_id in evidence_ids:
            evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
            if evidence is None:
                continue
            snippet = str(getattr(evidence, "snippet_text", "") or "")
            if assess_web_fetch_health(snippet, min_chars=1).state == "blocked":
                blocked.append(evidence_id)
        return blocked

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
        if question_id not in {str(item) for item in source_set.get("question_ids") or []}:
            return {
                "accepted": False,
                "error": "question_id must belong to the current source set",
                "question_id": question_id,
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

        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
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
        evidence_ids = public_api._dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
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
        if status in {"covered", "partial", "conflicted"} and not source_ids:
            return {"accepted": False, "error": f"{status} outcome requires at least one source_ref"}
        if status in {"covered", "partial", "conflicted"} and not evidence_ids:
            return {"accepted": False, "error": f"{status} outcome requires saved evidence"}
        if status == "covered" and not semantic_fields["coverage_summary"]:
            return {"accepted": False, "error": "covered outcome requires orientation_summary"}
        if status == "partial" and not (
            semantic_fields["coverage_summary"] and semantic_fields["missing_detail"]
        ):
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
            assessments_by_ref = {
                str(item.get("source_ref") or ""): item
                for item in current.get("source_assessments") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
            }
            relevant_refs = source_refs or [str(item) for item in source_set.get("source_refs") or []]
            has_representation_blocker = any(
                str((assessments_by_ref.get(source_ref) or {}).get("representation_status") or "")
                in {"partial", "not_target_content", "unreadable"}
                for source_ref in relevant_refs
            )
            if not has_representation_blocker:
                return {
                    "accepted": False,
                    "error": (
                        "deferred outcome requires a checked partial/not_target_content/unreadable representation; "
                        "use missing for checked absence or needs_repair for unfinished work"
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
            "orientation_summary": semantic_fields["coverage_summary"],
            "missing_detail": semantic_fields["missing_detail"],
            "conflict_summary": semantic_fields["conflict_summary"],
            "deferred_reason": semantic_fields["deferred_reason"],
            "confidence": _confidence(args.get("confidence")),
            "next_action": next_action,
            "updated_at": public_api._now(),
        }
        current["coverage_outcomes"] = [
            item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and str(item.get("outcome_id") or "") != outcome_id
        ]
        current["coverage_outcomes"].append(outcome)
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
        question_outcomes = [
            item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
        ]
        outcome_statuses = {str(item.get("status") or "") for item in question_outcomes}
        if status == "missing" and outcome_statuses != {"missing"}:
            return {
                "accepted": False,
                "error": "missing_synthesis_requires_all_set_outcomes_missing",
                "outcome_statuses": sorted(outcome_statuses),
            }
        if status == "deferred" and (
            "deferred" not in outcome_statuses
            or not outcome_statuses.issubset({"missing", "deferred"})
        ):
            return {
                "accepted": False,
                "error": "deferred_synthesis_cannot_discard_active_set_outcomes",
                "outcome_statuses": sorted(outcome_statuses),
            }
        next_action = str(args.get("next_action") or "research").strip()
        if next_action not in {"research", "source_acquisition", "omit", "none"}:
            return {"accepted": False, "error": "invalid next_action"}
        if next_action == "source_acquisition":
            if status == "covered":
                return {
                    "accepted": False,
                    "error": "covered_synthesis_cannot_request_source_acquisition",
                }
            assigned_sets = [
                item
                for item in current.get("source_sets") or []
                if isinstance(item, dict)
                and question_id in {str(value) for value in item.get("question_ids") or []}
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
            item
            for item in question_outcomes
            if str(item.get("status") or "") in {"covered", "partial", "conflicted"}
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
        manual_lineage = "source_refs" in args or "evidence_ids" in args
        source_refs = public_api._dedupe(
            [
                str(item)
                for item in (
                    args.get("source_refs") if manual_lineage else derived_source_refs
                )
                or []
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
            *[
                str(source_ref)
                for outcome in active_outcomes
                for source_ref in outcome.get("source_refs") or []
            ],
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
                ref for ref in source_refs if ref not in assessments_by_ref or not research_eligible(assessments_by_ref[ref])
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
                for item in (
                    args.get("evidence_ids") if manual_lineage else derived_evidence_ids
                )
                or []
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
        unreviewed_evidence_ids = sorted(
            set(evidence_ids) - allowed_evidence_ids
        )
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
        if status == "partial" and not (
            semantic_fields["coverage_summary"] and semantic_fields["missing_detail"]
        ):
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

    async def _validate_set_evidence_ids(
        self,
        *,
        source_set: dict[str, Any],
        evidence_ids: list[str],
        required_source_id: str | None = None,
        required_source_ids: set[str] | None = None,
        selection_purpose: str,
        selection_role: str,
    ) -> dict[str, Any] | None:
        if not evidence_ids:
            return None
        if self.evidence_toolset is None:
            return {
                "accepted": False,
                "error": "Evidence validation is unavailable; evidence-backed triage state was not persisted",
                "invalid_evidence_ids": evidence_ids,
            }
        invalid: list[str] = []
        ungrounded: list[str] = []
        allowed_refs = set(source_set.get("source_refs") or [])
        for evidence_id in evidence_ids:
            evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
            if evidence is None:
                invalid.append(evidence_id)
                continue
            evidence_ref = await self.evidence_toolset._source_ref_for_id(evidence.source_id)
            if (
                evidence_ref not in allowed_refs
                or (required_source_id is not None and str(evidence.source_id) != required_source_id)
                or (required_source_ids is not None and str(evidence.source_id) not in required_source_ids)
            ):
                invalid.append(evidence_id)
                continue
            if not str(getattr(evidence, "snippet_text", "") or "").strip() or (
                _unsupported_model_read_selected_as_support(
                    evidence,
                    purpose=selection_purpose,
                    role=selection_role,
                )
            ):
                ungrounded.append(evidence_id)
        if invalid:
            return {
                "accepted": False,
                "error": "evidence_ids must come from the relevant source(s) in the current set",
                "invalid_evidence_ids": invalid,
            }
        if ungrounded:
            return {
                "accepted": False,
                "error": "evidence_ids must contain grounded source text that supports the persisted triage status",
                "invalid_evidence_ids": ungrounded,
            }
        return None

    async def _select_validated_evidence_ids(
        self,
        *,
        evidence_ids: list[str],
        purpose: str,
        role: str,
        rationale: str,
    ) -> dict[str, Any] | None:
        if not evidence_ids:
            return None
        selector = getattr(self.evidence_toolset, "_select_evidence_ids_for_write", None)
        if not callable(selector):
            return {
                "accepted": False,
                "error": "Evidence selection is unavailable; evidence-backed triage state was not persisted",
                "invalid_evidence_ids": evidence_ids,
            }
        invalid = await selector(
            evidence_ids,
            purpose=purpose,
            role=role,
            rationale=rationale,
        )
        if invalid:
            return {
                "accepted": False,
                "error": "evidence_ids could not be selected for the persisted triage status",
                "invalid_evidence_ids": invalid,
            }
        return None

    @staticmethod
    def _recompute_source_set_question_links(
        workspace: dict[str, Any],
        source_set: dict[str, Any],
    ) -> None:
        """Derive emergent question links from current assessments, not history."""

        set_id = str(source_set.get("set_id") or "")
        if "planned_question_ids" in source_set:
            planned_question_ids = workspace_api._dedupe(
                [str(item) for item in source_set.get("planned_question_ids") or []]
            )
        else:
            stored_cross_ids = {
                str(item) for item in source_set.get("cross_question_ids") or []
            }
            planned_question_ids = workspace_api._dedupe(
                [
                    str(item)
                    for item in source_set.get("question_ids") or []
                    if str(item) not in stored_cross_ids
                ]
            )
        assessment_question_ids = workspace_api._dedupe(
            [
                str(question_id)
                for assessment in workspace.get("source_assessments") or []
                if isinstance(assessment, dict) and str(assessment.get("set_id") or "") == set_id
                for question_id in assessment.get("coverage_question_ids") or []
            ]
        )
        cross_question_ids = [
            question_id
            for question_id in assessment_question_ids
            if question_id not in set(planned_question_ids)
        ]
        previous_question_ids = {
            str(item) for item in source_set.get("question_ids") or []
        }
        source_set["planned_question_ids"] = planned_question_ids
        source_set["cross_question_ids"] = cross_question_ids
        source_set["question_ids"] = workspace_api._dedupe(
            [*planned_question_ids, *cross_question_ids]
        )
        removed_question_ids = previous_question_ids - set(source_set["question_ids"])
        if removed_question_ids:
            workspace["coverage_outcomes"] = [
                item
                for item in workspace.get("coverage_outcomes") or []
                if not isinstance(item, dict)
                or str(item.get("set_id") or "") != set_id
                or str(item.get("question_id") or "") not in removed_question_ids
            ]

    @staticmethod
    def _recompute_all_set_evidence_ids(workspace: dict[str, Any]) -> None:
        evidence_by_set: dict[str, list[str]] = {}
        for field in ("source_assessments", "coverage_outcomes"):
            for item in workspace.get(field) or []:
                if not isinstance(item, dict):
                    continue
                set_id = str(item.get("set_id") or "")
                if set_id:
                    evidence_by_set.setdefault(set_id, []).extend(
                        str(value) for value in item.get("evidence_ids") or []
                    )
        recomputed_sets: list[dict[str, Any]] = []
        for raw_source_set in workspace.get("source_sets") or []:
            source_set = dict(raw_source_set)
            set_id = str(source_set.get("set_id") or "")
            evidence_ids = list(evidence_by_set.get(set_id, []))
            for field in ("observations", "written_actions"):
                for item in source_set.get(field) or []:
                    if isinstance(item, dict):
                        evidence_ids.extend(str(value) for value in item.get("evidence_ids") or [])
            source_set["evidence_ids"] = workspace_api._dedupe(evidence_ids)
            recomputed_sets.append(source_set)
        workspace["source_sets"] = recomputed_sets

    @staticmethod
    def _assessment_deferral_reason(assessment: dict[str, Any], *, fallback: str = "") -> str:
        return next(
            (
                value
                for value in (
                    str(assessment.get("disposition_reason") or "").strip(),
                    str(assessment.get("representation_reason") or "").strip(),
                    str(assessment.get("relevance_reason") or "").strip(),
                    str(assessment.get("uniqueness_reason") or "").strip(),
                    fallback.strip(),
                )
                if value
            ),
            "",
        )

    @classmethod
    def _sync_source_resolution_indexes(
        cls,
        workspace: dict[str, Any],
        *,
        reason_overrides: dict[str, str] | None = None,
    ) -> None:
        overrides = reason_overrides or {}
        existing_reasons = {
            str(item.get("source_ref") or ""): str(item.get("reason") or "").strip()
            for item in workspace.get("deferred_sources") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
        }
        set_reason_by_ref = {
            str(source_ref): str(source_set.get("deferred_reason") or "").strip()
            for source_set in workspace.get("source_sets") or []
            if isinstance(source_set, dict)
            for source_ref in source_set.get("source_refs") or []
        }
        assessment_by_ref = {
            str(item.get("source_ref") or ""): item
            for item in workspace.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
        }
        deferred_entries: list[dict[str, str]] = []
        for source_ref in workspace.get("source_order") or []:
            source_ref = str(source_ref)
            assessment = assessment_by_ref.get(source_ref)
            if not assessment or str(assessment.get("disposition") or "") != "deferred":
                continue
            reason = (
                str(overrides.get(source_ref) or "").strip()
                or cls._assessment_deferral_reason(
                    assessment,
                    fallback=set_reason_by_ref.get(source_ref, ""),
                )
                or existing_reasons.get(source_ref, "")
            )
            deferred_entries.append({"source_ref": source_ref, "reason": reason})
        workspace["deferred_sources"] = deferred_entries

        deferred_refs = {item["source_ref"] for item in deferred_entries}
        mapped_refs: list[str] = []
        for source_set in workspace.get("source_sets") or []:
            if not isinstance(source_set, dict) or str(source_set.get("status") or "") != "mapped":
                continue
            mapped_refs.extend(
                source_ref
                for source_ref in workspace_api._source_set_covered_refs(source_set)
                if source_ref not in deferred_refs
            )
        workspace["mapped_sources"] = workspace_api._dedupe(mapped_refs)

    async def record_triage_observation_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        if not set_id:
            return {"accepted": False, "error": "record_triage_observation requires a current set_id"}
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        invalid = [ref for ref in source_refs if ref not in set(source_set.get("source_refs") or [])]
        if invalid:
            return {
                "accepted": False,
                "error": "Observation source_refs must belong to the current source set",
                "invalid_source_refs": invalid,
            }
        if (
            str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE)
            == SOURCE_SET_KIND_ORIENTATION
            and len(source_refs) > 1
        ):
            return {
                "accepted": False,
                "error": "orientation_observation_requires_one_source",
                "message": (
                    "A neutral orientation queue is not semantic lineage. Persist one observation per source; "
                    "use an evidence-backed relationship or cluster only after a real relationship is established."
                ),
                "source_refs": source_refs,
            }
        if source_set.get("repair_targeted"):
            allowed_repair_refs = {
                str(item) for item in source_set.get("repair_source_refs") or []
            }
            out_of_scope_refs = sorted(set(source_refs) - allowed_repair_refs)
            if not source_refs or out_of_scope_refs:
                return {
                    "accepted": False,
                    "error": "observation is outside the targeted repair scope",
                    "invalid_source_refs": out_of_scope_refs or source_refs,
                    "allowed_source_refs": sorted(allowed_repair_refs),
                }
        if not source_refs:
            return {"accepted": False, "error": "record_triage_observation requires at least one source_ref"}
        note = str(args.get("note") or "").strip()
        if not note:
            return {"accepted": False, "error": "note is required"}
        field_error = semantic_field_error({"note": note}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["note"])
        if field_error:
            return field_error
        pending_questions = [str(item).strip() for item in args.get("pending_questions") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="pending_questions",
            values=pending_questions,
            max_items=TRIAGE_PENDING_QUESTION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["pending_question"],
        )
        if list_error:
            return list_error
        evidence_ids = public_api._dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        if evidence_ids and self.evidence_toolset is not None:
            invalid_evidence_ids: list[str] = []
            allowed_refs = set(source_set.get("source_refs") or [])
            for evidence_id in evidence_ids:
                evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                if evidence is None:
                    invalid_evidence_ids.append(evidence_id)
                    continue
                evidence_ref = await self.evidence_toolset._source_ref_for_id(evidence.source_id)
                if evidence_ref not in allowed_refs:
                    invalid_evidence_ids.append(evidence_id)
            if invalid_evidence_ids:
                return {
                    "accepted": False,
                    "error": "observation evidence_ids must be observed evidence from the current source set",
                    "invalid_evidence_ids": invalid_evidence_ids,
                }
        observation = {
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "note": note,
            "pending_questions": pending_questions,
            "created_at": public_api._now(),
        }
        source_set.setdefault("observations", []).append(observation)
        source_set["evidence_ids"] = public_api._dedupe([*(source_set.get("evidence_ids") or []), *evidence_ids])
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "set_id": set_id,
            "observation_count": len(source_set.get("observations") or []),
            "evidence_count": len(evidence_ids),
        }

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
        if status not in {"mapped", "deferred", "needs_repair"}:
            return {"accepted": False, "error": "status must be mapped, deferred, or needs_repair"}
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
        if missing_assessment_refs and status != "needs_repair":
            return {
                "accepted": False,
                "error": "Every source in the set needs a persisted source assessment",
                "missing_source_refs": missing_assessment_refs,
            }
        unfinished_assessment_refs = sorted(
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if assessment.get("processing_status") is not None
            and str(assessment.get("processing_status")) != "resolved"
        )
        if status in {"mapped", "deferred"} and unfinished_assessment_refs:
            return {
                "accepted": False,
                "error": "mapped/deferred set requires completed assessments; use needs_repair for unfinished work",
                "source_refs": unfinished_assessment_refs,
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
        if missing_outcome_ids and status != "needs_repair":
            return {
                "accepted": False,
                "error": "Every assigned coverage question needs a final outcome",
                "missing_question_ids": missing_outcome_ids,
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
        if status in {"deferred", "needs_repair"} and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred/needs_repair status"}
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


__all__ = ["SourceTriageWorkspaceToolHandlers"]


def _confidence(value: Any) -> float:
    return max(0.0, min(float(0.5 if value is None else value), 1.0))
