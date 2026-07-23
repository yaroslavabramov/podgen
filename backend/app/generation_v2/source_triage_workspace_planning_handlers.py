from __future__ import annotations

import json
import hashlib
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS,
    TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT,
    TRIAGE_CAPSULE_PAGE_MAX_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_workspace_state import (
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
)
from app.generation_v2.source_triage_plan_validation import (
    question_parent_cycle,
    triage_plan_finalization_error,
    validate_triage_plan,
)


ORIENTATION_SET_ID = "orientation_initial_reading"


class SourceTriageWorkspacePlanningHandlersMixin:
    async def upsert_coverage_question_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        question = str(args.get("question") or "").strip()
        if not question:
            return {"accepted": False, "error": "question is required"}
        field_error = semantic_field_error(
            {
                "question": question,
                "question_rationale": str(args.get("rationale") or "").strip(),
            },
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["question", "question_rationale"],
        )
        if field_error:
            return field_error
        kind = str(args.get("kind") or "other").strip()
        importance = str(args.get("importance") or "supporting").strip()
        allowed_kinds = {
            "context",
            "origin",
            "mechanism",
            "cause",
            "change",
            "impact",
            "exception",
            "verification",
            "other",
        }
        if kind not in allowed_kinds:
            return {"accepted": False, "error": "invalid coverage question kind"}
        if importance not in {"critical", "supporting", "exploratory"}:
            return {"accepted": False, "error": "invalid coverage question importance"}
        requested_id = str(args.get("question_id") or "").strip()
        if requested_id:
            question_id = public_api._stable_set_id(requested_id, question, 1)
        else:
            digest = hashlib.sha256(question.casefold().encode("utf-8")).hexdigest()[:10]
            question_id = f"q_{digest}"
        parent_question_id = str(args.get("parent_question_id") or "").strip() or None
        existing_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        if parent_question_id == question_id:
            return {"accepted": False, "error": "parent_question_id cannot reference the question itself"}
        if parent_question_id and parent_question_id not in existing_ids:
            return {"accepted": False, "error": "parent_question_id must reference an existing question"}
        raw_requirements = args.get("answer_requirements") or []
        if not isinstance(raw_requirements, list) or not raw_requirements:
            return {
                "accepted": False,
                "error": "answer_requirements must contain the semantic parts of a complete answer",
            }
        answer_requirements: list[dict[str, str]] = []
        seen_requirement_ids: set[str] = set()
        for index, raw_requirement in enumerate(raw_requirements, start=1):
            if not isinstance(raw_requirement, dict):
                return {"accepted": False, "error": "answer_requirements items must be objects"}
            description = str(raw_requirement.get("description") or "").strip()
            rationale = str(raw_requirement.get("rationale") or "").strip()
            completion_criterion = str(raw_requirement.get("completion_criterion") or "").strip()
            if not description:
                return {
                    "accepted": False,
                    "error": "Each answer requirement needs a description",
                    "requirement_index": index - 1,
                }
            if "completion_criterion" in raw_requirement and not completion_criterion:
                return {
                    "accepted": False,
                    "error": "Each answer requirement needs a non-empty completion_criterion",
                    "requirement_index": index - 1,
                }
            requirement_error = semantic_field_error(
                {
                    "requirement_description": description,
                    "requirement_rationale": rationale,
                    "completion_criterion": completion_criterion,
                },
                {
                    **TRIAGE_SEMANTIC_FIELD_LIMITS,
                    "completion_criterion": TRIAGE_SEMANTIC_FIELD_LIMITS[
                        "requirement_completion_criterion"
                    ],
                },
                ["requirement_description", "requirement_rationale", "completion_criterion"],
            )
            if requirement_error:
                return requirement_error
            requested_requirement_id = str(raw_requirement.get("requirement_id") or "").strip()
            requirement_prefix = f"{question_id}:"
            if ":" in requested_requirement_id:
                if not requested_requirement_id.startswith(requirement_prefix):
                    return {
                        "accepted": False,
                        "error": "requirement_id belongs to another question",
                        "requirement_id": requested_requirement_id,
                        "question_id": question_id,
                    }
                if requested_requirement_id == requirement_prefix:
                    return {
                        "accepted": False,
                        "error": "requirement_id suffix is empty",
                        "next_action": "Omit requirement_id to create a stable one.",
                    }
                requirement_id = requested_requirement_id
            elif requested_requirement_id:
                requirement_id = public_api._stable_set_id(requested_requirement_id, description, index)
            else:
                requirement_id = (
                    f"{question_id}:r_{hashlib.sha256(description.casefold().encode('utf-8')).hexdigest()[:10]}"
                )
            if requirement_id in seen_requirement_ids:
                return {
                    "accepted": False,
                    "error": "answer requirement ids must be unique within a question",
                    "requirement_id": requirement_id,
                }
            seen_requirement_ids.add(requirement_id)
            answer_requirements.append(
                {
                    "requirement_id": requirement_id,
                    "description": description,
                    "rationale": rationale,
                    "completion_criterion": completion_criterion or description,
                }
            )
        item = {
            "question_id": question_id,
            "question": question,
            "kind": kind,
            "importance": importance,
            "rationale": str(args.get("rationale") or "").strip(),
            "parent_question_id": parent_question_id,
            "origin": "planned",
            "answer_requirements": answer_requirements,
        }
        updated_questions = [
            item if existing.get("question_id") == question_id else existing
            for existing in current.get("coverage_questions") or []
        ]
        if question_id not in existing_ids:
            updated_questions.append(item)
        cycle = question_parent_cycle(updated_questions)
        if cycle:
            return {
                "accepted": False,
                "error": "coverage question hierarchy cannot contain a cycle",
                "question_ids": cycle,
            }
        current["coverage_questions"] = updated_questions
        # A planner rewrite changes the authoritative question stream. Any
        # previous finalizer read ledger belongs to the older stream.
        current.pop("source_triage_finalizer_reads", None)
        current["status"] = "planning"
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "question_id": question_id,
            "coverage_question_count": len(current["coverage_questions"]),
            "answer_requirement_count": len(answer_requirements),
        }

    async def list_source_capsules_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = [str(item) for item in current.get("source_order") or []]
        requested_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_refs = [ref for ref in requested_refs if ref not in source_order]
        if invalid_refs:
            return {
                "accepted": False,
                "error": "source_refs must exist in the triage workspace",
                "invalid_source_refs": invalid_refs,
            }
        if len(requested_refs) > 1:
            return {"accepted": False, "error": "request one source_ref at a time"}
        limit = max(1, min(int(args.get("limit") or TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT), TRIAGE_CAPSULE_PAGE_MAX_LIMIT))
        cursor = str(args.get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0
        refs = requested_refs or source_order[offset : offset + limit]
        if self.evidence_toolset is not None:
            current = await public_api.ensure_capsules_for_refs(
                self.db,
                run=self.run,
                workspace=current,
                inventory=self.inventory,
                evidence_toolset=self.evidence_toolset,
                source_refs=refs,
            )
        capsules = current.get("source_capsules") or {}
        page_capsules: list[dict[str, Any]] = []
        for ref in refs:
            capsule = capsules.get(ref) or public_api._metadata_only_capsule(self.inventory, ref)
            candidate = [*page_capsules, capsule]
            encoded = json.dumps(
                public_api._capsule_page_payload(
                    source_count=len(source_order),
                    capsules=candidate,
                    next_cursor=(
                        ""
                        if requested_refs
                        else str(offset + len(candidate))
                        if offset + len(candidate) < len(source_order)
                        else ""
                    ),
                ),
                ensure_ascii=False,
                default=str,
            )
            if page_capsules and len(encoded) > TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS:
                break
            page_capsules = candidate
        returned_count = len(page_capsules)
        next_cursor = (
            ""
            if requested_refs
            else str(offset + returned_count)
            if offset + returned_count < len(source_order)
            else ""
        )
        if requested_refs and returned_count:
            current["planner_inspected_source_refs"] = public_api._dedupe(
                [
                    *[str(item) for item in current.get("planner_inspected_source_refs") or []],
                    *[str(item.get("source_ref") or "") for item in page_capsules if isinstance(item, dict)],
                ]
            )
            await public_api.save_source_triage_workspace(
                self.db,
                generation_run_id=self.run.id,
                workspace=current,
            )
        return {
            **public_api._capsule_page_payload(
                source_count=len(source_order),
                capsules=page_capsules,
                next_cursor=next_cursor,
            ),
            "returned_count": returned_count,
            "page_limit": limit,
            "requested_source_refs": requested_refs,
        }

    async def route_sources_for_initial_reading_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Persist a non-editorial work queue for sources the planner could not classify."""

        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = {str(item) for item in current.get("source_order") or []}
        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        if not source_refs:
            return {
                "accepted": False,
                "error": "route_sources_for_initial_reading requires at least one source_ref",
            }
        invalid_refs = sorted(set(source_refs) - source_order)
        if invalid_refs:
            return {
                "accepted": False,
                "error": "source_refs must exist in the triage workspace",
                "invalid_source_refs": invalid_refs,
            }
        inspected_refs = {str(item) for item in current.get("planner_inspected_source_refs") or []}
        uninspected_refs = [ref for ref in source_refs if ref not in inspected_refs]
        if uninspected_refs:
            return {
                "accepted": False,
                "error": (
                    "Each source_ref must first be explicitly inspected with list_source_capsules(source_refs=[...])"
                ),
                "uninspected_source_refs": uninspected_refs,
            }

        existing_orientation = next(
            (
                dict(item)
                for item in current.get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == ORIENTATION_SET_ID
            ),
            None,
        )
        if (
            existing_orientation
            and str(existing_orientation.get("set_kind") or SOURCE_SET_KIND_COVERAGE) != SOURCE_SET_KIND_ORIENTATION
        ):
            return {
                "accepted": False,
                "error": "The reserved initial-reading set_id is already used by a coverage set",
            }
        conflicting_assignments = sorted(
            {
                ref
                for item in current.get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") != ORIENTATION_SET_ID
                for ref in source_refs
                if ref in {str(value) for value in item.get("source_refs") or []}
            }
        )
        if conflicting_assignments:
            return {
                "accepted": False,
                "error": "A source already assigned to a coverage set cannot also enter initial reading",
                "source_refs": conflicting_assignments,
            }

        source_set = existing_orientation or {
            "set_id": ORIENTATION_SET_ID,
            "label": "Initial reading",
            "purpose": "Read each source before assigning topical relevance.",
            "source_refs": [],
            "status": "planned",
            "rationale": "Backend-routed after explicit capsule inspection.",
            "question_ids": [],
            "question_assignments": [],
            "planned_question_ids": [],
            "cross_question_ids": [],
            "priority": "normal",
            "findings_summary": "",
            "evidence_ids": [],
            "written_actions": [],
            "observations": [],
            "deferred_reason": "",
            "repair": False,
            "updated_at": public_api._now(),
        }
        source_set["set_kind"] = SOURCE_SET_KIND_ORIENTATION
        source_set["source_refs"] = public_api._dedupe(
            [*[str(item) for item in source_set.get("source_refs") or []], *source_refs]
        )
        source_set["planned_question_ids"] = []
        source_set["cross_question_ids"] = []
        source_set["question_ids"] = []
        source_set["updated_at"] = public_api._now()
        sets = [
            item
            for item in current.get("source_sets") or []
            if not isinstance(item, dict) or str(item.get("set_id") or "") != ORIENTATION_SET_ID
        ]
        sets.append(source_set)
        current["source_sets"] = sets
        current["status"] = "planning"
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "set_id": ORIENTATION_SET_ID,
            "source_count": len(source_set["source_refs"]),
            "routed_source_refs": source_refs,
            "planned_set_count": len(sets),
        }

    async def upsert_triage_source_set_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        if "repair" in args:
            return {
                "accepted": False,
                "error": "Planner-created source sets cannot be marked as repair",
            }
        source_order = set(str(item) for item in current.get("source_order") or [])
        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {
                "accepted": False,
                "error": "source_refs must exist in the triage workspace",
                "invalid_source_refs": invalid,
            }
        if not source_refs:
            return {"accepted": False, "error": "upsert_triage_source_set requires at least one source_ref"}
        orientation_refs = {
            str(ref)
            for item in current.get("source_sets") or []
            if isinstance(item, dict)
            and str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_ORIENTATION
            for ref in item.get("source_refs") or []
        }
        already_routed_refs = sorted(set(source_refs).intersection(orientation_refs))
        if already_routed_refs:
            return {
                "accepted": False,
                "error": "A source routed for initial reading cannot also enter a coverage set",
                "source_refs": already_routed_refs,
            }
        label = str(args.get("label") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not label or not purpose:
            return {"accepted": False, "error": "label and purpose are required"}
        rationale = str(args.get("rationale") or "").strip()
        priority = str(args.get("priority") or "normal").strip() or "normal"
        field_error = semantic_field_error(
            {"label": label, "purpose": purpose, "rationale": rationale, "priority": priority},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["label", "purpose", "rationale", "priority"],
        )
        if field_error:
            return field_error
        raw_assignments = args.get("question_assignments")
        if raw_assignments is None:
            raw_assignments = [
                {"question_id": str(item), "requirement_ids": []} for item in args.get("question_ids") or []
            ]
        if not isinstance(raw_assignments, list):
            return {"accepted": False, "error": "question_assignments must be an array"}
        question_by_id = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        question_assignments: list[dict[str, Any]] = []
        seen_assignment_question_ids: set[str] = set()
        for raw_assignment in raw_assignments:
            if not isinstance(raw_assignment, dict):
                return {"accepted": False, "error": "question_assignments items must be objects"}
            question_id = str(raw_assignment.get("question_id") or "").strip()
            question = question_by_id.get(question_id)
            if question is None:
                return {
                    "accepted": False,
                    "error": "question_assignments must reference persisted coverage questions",
                    "unknown_question_ids": [question_id],
                }
            if question_id in seen_assignment_question_ids:
                return {
                    "accepted": False,
                    "error": "Use exactly one question_assignment per question_id in a source set",
                    "question_id": question_id,
                    "next_action": (
                        "Merge that question's requirement_ids into one assignment and retry upsert_triage_source_set."
                    ),
                }
            seen_assignment_question_ids.add(question_id)
            known_requirement_ids = {
                str(item.get("requirement_id") or "")
                for item in question.get("answer_requirements") or []
                if isinstance(item, dict)
            }
            requested_requirement_ids = public_api._dedupe(
                [str(item) for item in raw_assignment.get("requirement_ids") or [] if str(item).strip()]
            )
            # Compatibility for old internal callers: question_ids means all
            # requirements of the question. New model calls are explicit.
            requirement_ids = requested_requirement_ids or sorted(known_requirement_ids)
            unknown_requirement_ids = sorted(set(requirement_ids) - known_requirement_ids)
            if unknown_requirement_ids:
                return {
                    "accepted": False,
                    "error": "requirement_ids must belong to their assigned question",
                    "question_id": question_id,
                    "unknown_requirement_ids": unknown_requirement_ids,
                }
            if not requirement_ids and known_requirement_ids:
                return {
                    "accepted": False,
                    "error": "Each question assignment must include at least one answer requirement",
                    "question_id": question_id,
                }
            question_assignments.append({"question_id": question_id, "requirement_ids": requirement_ids})
        question_ids = public_api._dedupe([str(item.get("question_id") or "") for item in question_assignments])
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        unknown_question_ids = sorted(set(question_ids) - known_question_ids)
        if unknown_question_ids:
            return {
                "accepted": False,
                "error": "question_ids must reference persisted coverage questions",
                "unknown_question_ids": unknown_question_ids,
            }
        if not question_ids:
            return {
                "accepted": False,
                "error": "Every source set must be attached to at least one persisted coverage question",
            }
        set_id = public_api._stable_set_id(
            str(args.get("set_id") or ""), label, len(current.get("source_sets") or []) + 1
        )
        source_set = {
            "set_id": set_id,
            "set_kind": SOURCE_SET_KIND_COVERAGE,
            "label": label,
            "purpose": purpose,
            "source_refs": source_refs,
            "status": "planned",
            "rationale": rationale,
            "question_ids": question_ids,
            "question_assignments": question_assignments,
            "planned_question_ids": question_ids,
            "cross_question_ids": [],
            "priority": priority,
            "findings_summary": "",
            "evidence_ids": [],
            "written_actions": [],
            "observations": [],
            "deferred_reason": "",
            "repair": False,
            "updated_at": public_api._now(),
        }
        sets = [item for item in current.get("source_sets") or [] if item.get("set_id") != set_id]
        existing_same_id = next(
            (
                item
                for item in current.get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
            ),
            None,
        )
        if (
            existing_same_id
            and str(existing_same_id.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_ORIENTATION
        ):
            return {
                "accepted": False,
                "error": "Initial-reading sets cannot be replaced with upsert_triage_source_set",
            }
        sets.append(source_set)
        current["source_sets"] = sets
        current["status"] = "planning"
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "source_count": len(source_refs), "planned_set_count": len(sets)}

    async def finalize_triage_workspace_plan_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        if args.get("deferred_sources"):
            return {
                "accepted": False,
                "error": "The planner cannot defer sources; assign every source to exactly one set",
            }
        central_question_id = str(args.get("central_question_id") or "").strip()
        validation = validate_triage_plan(
            current,
            central_question_id=central_question_id,
        )
        validation_error = triage_plan_finalization_error(validation)
        if validation_error:
            return validation_error
        source_sets = [dict(item) for item in current.get("source_sets") or []]
        questions = [
            item
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        ]
        summary = str(args.get("summary") or "").strip()
        field_error = semantic_field_error({"plan_summary": summary}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["plan_summary"])
        if field_error:
            return field_error
        current["status"] = "planned"
        current["central_question_id"] = central_question_id
        current["plan_summary"] = summary
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "status": "planned",
            "source_set_count": len(source_sets),
            "coverage_question_count": len(questions),
        }


__all__ = ["SourceTriageWorkspacePlanningHandlersMixin"]
