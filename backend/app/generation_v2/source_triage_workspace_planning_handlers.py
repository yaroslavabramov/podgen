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
        cycle = _question_parent_cycle(updated_questions)
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
        source_order = [str(item) for item in current.get("source_order") or []]
        source_sets = [dict(item) for item in current.get("source_sets") or []]
        if args.get("deferred_sources"):
            return {
                "accepted": False,
                "error": "The planner cannot defer sources; assign every source to exactly one set",
            }
        ref_counts = {ref: sum(ref in (item.get("source_refs") or []) for item in source_sets) for ref in source_order}
        duplicate_refs = sorted(ref for ref, count in ref_counts.items() if count > 1)
        missing = [ref for ref, count in ref_counts.items() if count == 0]
        if not source_sets:
            return {"accepted": False, "error": "Cannot finalize triage workspace without source sets"}
        if duplicate_refs:
            return {
                "accepted": False,
                "error": "Every source_ref must belong to exactly one source set",
                "duplicate_source_refs": duplicate_refs,
            }
        if missing:
            return {
                "accepted": False,
                "error": "Every source_ref must be assigned to exactly one source set",
                "missing_source_refs": missing,
            }
        questions = [item for item in current.get("coverage_questions") or [] if isinstance(item, dict)]
        if not questions:
            return {"accepted": False, "error": "Persist at least one coverage question before finalizing"}
        hierarchy_cycle = _question_parent_cycle(questions)
        if hierarchy_cycle:
            return {
                "accepted": False,
                "error": "coverage question hierarchy cannot contain a cycle",
                "question_ids": hierarchy_cycle,
            }
        known_question_ids = {str(item.get("question_id") or "") for item in questions}
        central_question_id = str(args.get("central_question_id") or "").strip()
        central_question = next(
            (item for item in questions if str(item.get("question_id") or "") == central_question_id),
            None,
        )
        if central_question is None:
            return {
                "accepted": False,
                "error": "central_question_id must reference a persisted coverage question",
                "central_question_id": central_question_id,
            }
        if str(central_question.get("parent_question_id") or "").strip():
            return {
                "accepted": False,
                "error": "central_question_id must reference a root question",
                "central_question_id": central_question_id,
            }
        if str(central_question.get("importance") or "") != "critical":
            return {
                "accepted": False,
                "error": "central_question_id must reference a critical question",
                "central_question_id": central_question_id,
            }
        invalid_set_kinds = [
            str(item.get("set_id") or "")
            for item in source_sets
            if str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE)
            not in {SOURCE_SET_KIND_COVERAGE, SOURCE_SET_KIND_ORIENTATION}
        ]
        coverage_sets = [
            item
            for item in source_sets
            if str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_COVERAGE
        ]
        orientation_sets = [
            item
            for item in source_sets
            if str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_ORIENTATION
        ]
        duplicate_assignment_pairs: list[dict[str, str]] = []
        for source_set in coverage_sets:
            seen_question_ids: set[str] = set()
            for assignment in source_set.get("question_assignments") or []:
                if not isinstance(assignment, dict):
                    continue
                question_id = str(assignment.get("question_id") or "")
                if question_id in seen_question_ids:
                    duplicate_assignment_pairs.append(
                        {
                            "set_id": str(source_set.get("set_id") or ""),
                            "question_id": question_id,
                        }
                    )
                seen_question_ids.add(question_id)
        referenced_question_ids = {
            str(question_id)
            for item in coverage_sets
            for question_id in item.get("planned_question_ids", item.get("question_ids") or [])
        }
        unknown_question_ids = sorted(referenced_question_ids - known_question_ids)
        unassigned_question_ids = sorted(known_question_ids - referenced_question_ids)
        required_assignment_pairs = {
            (str(question.get("question_id") or ""), str(requirement.get("requirement_id") or ""))
            for question in questions
            for requirement in question.get("answer_requirements") or []
            if isinstance(requirement, dict)
        }
        assigned_requirement_pairs = {
            (str(assignment.get("question_id") or ""), str(requirement_id))
            for item in coverage_sets
            for assignment in item.get("question_assignments") or []
            if isinstance(assignment, dict)
            for requirement_id in assignment.get("requirement_ids") or []
        }
        unassigned_requirements = sorted(required_assignment_pairs - assigned_requirement_pairs)
        unknown_assigned_requirements = sorted(assigned_requirement_pairs - required_assignment_pairs)
        known_source_refs = set(source_order)
        unknown_source_refs = sorted(
            {
                str(source_ref)
                for item in source_sets
                for source_ref in item.get("source_refs") or []
                if str(source_ref) not in known_source_refs
            }
        )
        coverage_sets_without_questions = [
            str(item.get("set_id") or "")
            for item in coverage_sets
            if not (item.get("planned_question_ids", item.get("question_ids") or []))
        ]
        orientation_sets_with_planned_questions = [
            str(item.get("set_id") or "")
            for item in orientation_sets
            if item.get("planned_question_ids") or item.get("question_ids")
        ]
        inspected_refs = {str(item) for item in current.get("planner_inspected_source_refs") or []}
        uninspected_orientation_refs = sorted(
            {
                str(ref)
                for item in orientation_sets
                for ref in item.get("source_refs") or []
                if str(ref) not in inspected_refs
            }
        )
        if (
            invalid_set_kinds
            or unknown_question_ids
            or unassigned_question_ids
            or unassigned_requirements
            or unknown_assigned_requirements
            or unknown_source_refs
            or coverage_sets_without_questions
            or orientation_sets_with_planned_questions
            or uninspected_orientation_refs
            or duplicate_assignment_pairs
        ):
            return {
                "accepted": False,
                "error": "The source-set plan has invalid question or initial-reading assignments",
                "invalid_source_set_ids": invalid_set_kinds,
                "unknown_question_ids": unknown_question_ids,
                "unassigned_question_ids": unassigned_question_ids,
                "unassigned_question_requirements": [
                    {"question_id": question_id, "requirement_id": requirement_id}
                    for question_id, requirement_id in unassigned_requirements
                ],
                "unknown_assigned_question_requirements": [
                    {"question_id": question_id, "requirement_id": requirement_id}
                    for question_id, requirement_id in unknown_assigned_requirements
                ],
                "unknown_source_refs": unknown_source_refs,
                "coverage_source_sets_without_question_ids": coverage_sets_without_questions,
                "orientation_source_sets_with_question_ids": orientation_sets_with_planned_questions,
                "uninspected_orientation_source_refs": uninspected_orientation_refs,
                "duplicate_question_assignments": duplicate_assignment_pairs,
            }
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


def _question_parent_cycle(questions: list[dict[str, Any]]) -> list[str]:
    parent_by_id = {
        str(item.get("question_id") or ""): str(item.get("parent_question_id") or "")
        for item in questions
        if isinstance(item, dict) and str(item.get("question_id") or "")
    }
    for start in parent_by_id:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current and current in parent_by_id:
            if current in positions:
                return path[positions[current] :] + [current]
            positions[current] = len(path)
            path.append(current)
            current = parent_by_id.get(current, "")
    return []
