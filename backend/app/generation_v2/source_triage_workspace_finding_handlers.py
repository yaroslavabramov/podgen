from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS
from app.generation_v2.source_triage_workspace_handler_helpers import semantic_json as _semantic_json
from app.generation_v2.source_triage_workspace_state import invalidate_coverage_syntheses
from app.generation_v2.source_triage_source_analysis_contract import (
    reconcile_source_assessment_findings,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context


class SourceTriageWorkspaceFindingHandlersMixin:
    async def update_source_finding_links_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        current = await workspace_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        finding_id = str(args.get("finding_id") or "").strip()
        finding = next(
            (
                item
                for item in current.get("source_findings") or []
                if isinstance(item, dict) and str(item.get("finding_id") or "") == finding_id
            ),
            None,
        )
        if finding is None:
            return {
                "accepted": False,
                "error": "Unknown source finding",
                "finding_id": finding_id,
                "next_action": "Page source_findings and copy an exact finding_id.",
            }
        raw_links = args.get("question_links")
        if not isinstance(raw_links, list):
            return {"accepted": False, "error": "question_links must be an array"}
        questions_by_id = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        }
        normalized_links: list[dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        for raw_link in raw_links:
            if not isinstance(raw_link, dict):
                return {"accepted": False, "error": "question_links items must be objects"}
            question_id = str(raw_link.get("question_id") or "").strip()
            question = questions_by_id.get(question_id)
            if question is None:
                return {
                    "accepted": False,
                    "error": "question_links must reference persisted questions",
                    "unknown_question_ids": [question_id],
                }
            if question_id in seen_question_ids:
                return {
                    "accepted": False,
                    "error": "Use one link record per question",
                    "question_id": question_id,
                }
            seen_question_ids.add(question_id)
            known_requirement_ids = {
                str(item.get("requirement_id") or "")
                for item in question.get("answer_requirements") or []
                if isinstance(item, dict)
            }
            requirement_ids = workspace_api._dedupe(
                [str(item) for item in raw_link.get("requirement_ids") or [] if str(item).strip()]
            )
            unknown_requirement_ids = sorted(set(requirement_ids) - known_requirement_ids)
            if unknown_requirement_ids or (known_requirement_ids and not requirement_ids):
                return {
                    "accepted": False,
                    "error": "Link the finding only to specific requirements of the referenced question",
                    "question_id": question_id,
                    "unknown_requirement_ids": unknown_requirement_ids,
                    "allowed_requirement_ids": sorted(known_requirement_ids),
                }
            rationale = str(raw_link.get("rationale") or "").strip()
            rationale_error = semantic_field_error(
                {"finding_link_rationale": rationale},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_link_rationale"],
            )
            if rationale_error:
                return rationale_error
            normalized_links.append(
                {
                    "question_id": question_id,
                    "requirement_ids": requirement_ids,
                    "rationale": rationale,
                }
            )
        old_links = [item for item in finding.get("question_links") or [] if isinstance(item, dict)]
        if _semantic_json(old_links) == _semantic_json(normalized_links):
            return {
                "accepted": True,
                "finding_id": finding_id,
                "changed": False,
                "question_ids": [str(item.get("question_id") or "") for item in normalized_links],
            }

        set_id = str(finding.get("set_id") or "")
        source_id = str(finding.get("source_id") or "")
        affected_question_ids = {
            *[str(item.get("question_id") or "") for item in old_links],
            *[str(item.get("question_id") or "") for item in normalized_links],
        } - {""}
        previous_finding = deepcopy(finding)
        next_revision = int(current.get("triage_semantic_revision") or 0) + 1
        finding["question_links"] = normalized_links
        finding["semantic_revision"] = next_revision
        finding["updated_at"] = workspace_api._now()
        source_findings = [
            item
            for item in current.get("source_findings") or []
            if isinstance(item, dict)
            and str(item.get("source_id") or "") == source_id
            and str(item.get("set_id") or "") == set_id
        ]
        for assessment in current.get("source_assessments") or []:
            if (
                isinstance(assessment, dict)
                and str(assessment.get("source_id") or "") == source_id
                and str(assessment.get("set_id") or "") == set_id
            ):
                reconcile_source_assessment_findings(
                    assessment,
                    findings=source_findings,
                )
                assessment["updated_at"] = workspace_api._now()
        source_set = workspace_api._workspace_set(current, set_id)
        if source_set is None:
            return {
                "accepted": False,
                "error": "Finding owner set no longer exists",
                "finding_id": finding_id,
                "set_id": set_id,
            }
        self._recompute_source_set_question_links(current, source_set)
        for outcome in current.get("coverage_outcomes") or []:
            if (
                isinstance(outcome, dict)
                and str(outcome.get("set_id") or "") == set_id
                and str(outcome.get("question_id") or "") in affected_question_ids
            ):
                outcome["is_stale"] = True
        source_set["updated_at"] = workspace_api._now()
        workspace_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        invalidate_coverage_syntheses(current)
        # The finalizer supplied the new links itself. Carry review credit only
        # when it had already paged every part of the prior finding.
        from app.generation_v2.source_triage_workspace_tools import (
            carry_finalizer_finding_review_forward,
        )

        carry_finalizer_finding_review_forward(
            current,
            previous_finding=previous_finding,
        )
        await workspace_api.save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=current,
        )
        return {
            "accepted": True,
            "finding_id": finding_id,
            "changed": True,
            "question_ids": [str(item.get("question_id") or "") for item in normalized_links],
            "stale_outcome_question_ids": sorted(affected_question_ids),
        }

    async def update_source_finding_relations_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        current = await workspace_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        finding_id = str(args.get("finding_id") or "").strip()
        finding = next(
            (
                item
                for item in current.get("source_findings") or []
                if isinstance(item, dict) and str(item.get("finding_id") or "") == finding_id
            ),
            None,
        )
        if finding is None:
            return {
                "accepted": False,
                "error": "Unknown source finding",
                "finding_id": finding_id,
                "next_action": "Page source_findings and copy an exact finding_id.",
            }
        raw_relations = args.get("source_relations")
        if not isinstance(raw_relations, list):
            return {"accepted": False, "error": "source_relations must be an array"}

        ref_context = build_source_ref_context(self.inventory)
        source_ref = str(finding.get("source_ref") or "").strip()
        source_id = str(finding.get("source_id") or "").strip()
        current_source_refs = {
            str(item) for item in current.get("source_order") or [] if str(item) in ref_context.source_id_by_ref
        }
        if not current_source_refs:
            current_source_refs = set(ref_context.source_id_by_ref)
        if source_ref not in current_source_refs or ref_context.source_id_by_ref.get(source_ref) != source_id:
            return {
                "accepted": False,
                "error": "Finding owner source is not in the current run corpus view",
                "finding_id": finding_id,
                "source_ref": source_ref,
                "next_action": "Do not repair a stale finding from another run or corpus revision.",
            }
        set_id = str(finding.get("set_id") or "")
        if workspace_api._workspace_set(current, set_id) is None:
            return {
                "accepted": False,
                "error": "Finding owner set no longer exists",
                "finding_id": finding_id,
                "set_id": set_id,
            }

        allowed_relationships = {
            "primary_to_secondary",
            "duplicate",
            "near_duplicate",
            "contradicts",
            "updates",
            "background_for",
        }
        normalized_relations: list[dict[str, Any]] = []
        seen_relations: set[tuple[str, str]] = set()
        for relation_index, raw_relation in enumerate(raw_relations):
            if not isinstance(raw_relation, dict):
                return {
                    "accepted": False,
                    "error": "source_relations items must be objects",
                    "relation_index": relation_index,
                }
            related_source_ref = str(raw_relation.get("related_source_ref") or "").strip()
            related_source_id = ref_context.source_id_by_ref.get(related_source_ref)
            if related_source_ref not in current_source_refs or not related_source_id:
                return {
                    "accepted": False,
                    "error": "related_source_ref must belong to the current run corpus view",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                    "next_action": "Copy an exact related_source_ref from the current source catalog.",
                }
            if related_source_ref == source_ref:
                return {
                    "accepted": False,
                    "error": "A source finding cannot relate its source to itself",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                }
            relationship = str(raw_relation.get("relationship") or "").strip()
            if relationship not in allowed_relationships:
                return {
                    "accepted": False,
                    "error": "relationship must use a canonical enum literal",
                    "relation_index": relation_index,
                    "relationship": relationship,
                    "allowed_relationships": sorted(allowed_relationships),
                }
            relation_key = (related_source_ref, relationship)
            if relation_key in seen_relations:
                return {
                    "accepted": False,
                    "error": "Use one relation record per related source and relationship",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                    "relationship": relationship,
                }
            seen_relations.add(relation_key)
            rationale = str(raw_relation.get("rationale") or "").strip()
            rationale_error = semantic_field_error(
                {"finding_relation_rationale": rationale},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_relation_rationale"],
            )
            if rationale_error:
                return {**rationale_error, "relation_index": relation_index}
            evidence_ids = workspace_api._dedupe(
                [str(item) for item in raw_relation.get("evidence_ids") or [] if str(item).strip()]
            )
            if not evidence_ids:
                return {
                    "accepted": False,
                    "error": "An established source relation requires exact saved evidence",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                    "next_action": "Read exact material from the related sources, or remove the relation.",
                }
            evidence_error = await self._validate_set_evidence_ids(
                source_set={"source_refs": [source_ref, related_source_ref]},
                evidence_ids=evidence_ids,
                required_source_ids={source_id, related_source_id},
                selection_purpose="source_relationship",
                selection_role="triage_source_relationship",
            )
            if evidence_error:
                relation_error = {**evidence_error, "relation_index": relation_index}
                if evidence_error.get("error") == (
                    "evidence_ids must come from the relevant source(s) in the current set"
                ):
                    relation_error["error"] = (
                        "Relation evidence_ids must belong to the finding source or related source in this run"
                    )
                    relation_error["next_action"] = (
                        "Use exact saved evidence from these two source refs, or remove the relation."
                    )
                return relation_error
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(evidence_ids)
            if blocked_evidence_ids:
                return {
                    "accepted": False,
                    "error": "Shell/error/access-page evidence cannot establish a source relation",
                    "relation_index": relation_index,
                    "invalid_evidence_ids": blocked_evidence_ids,
                }
            evidence_source_ids: set[str] = set()
            for evidence_id in evidence_ids:
                evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                if evidence is not None:
                    evidence_source_ids.add(str(evidence.source_id))
            missing_evidence_source_refs = [
                ref
                for ref, expected_source_id in (
                    (source_ref, source_id),
                    (related_source_ref, related_source_id),
                )
                if expected_source_id not in evidence_source_ids
            ]
            if missing_evidence_source_refs:
                return {
                    "accepted": False,
                    "error": "A source relation requires grounded non-shell evidence from both sources",
                    "relation_index": relation_index,
                    "missing_evidence_source_refs": missing_evidence_source_refs,
                    "next_action": (
                        "Read exact material from each listed source_ref, then retry with both evidence_ids; "
                        "otherwise remove the relation."
                    ),
                }
            normalized_relations.append(
                {
                    "related_source_ref": related_source_ref,
                    "related_source_id": related_source_id,
                    "relationship": relationship,
                    "rationale": rationale,
                    "evidence_ids": evidence_ids,
                }
            )

        normalized_relations.sort(
            key=lambda item: (
                str(item.get("related_source_ref") or ""),
                str(item.get("relationship") or ""),
            )
        )
        old_relations = [item for item in finding.get("source_relations") or [] if isinstance(item, dict)]
        if _semantic_json(old_relations) == _semantic_json(normalized_relations):
            return {
                "accepted": True,
                "finding_id": finding_id,
                "changed": False,
                "relation_count": len(normalized_relations),
            }

        relation_evidence_ids = workspace_api._dedupe(
            [
                str(evidence_id)
                for relation in normalized_relations
                for evidence_id in relation.get("evidence_ids") or []
            ]
        )
        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=relation_evidence_ids,
            purpose="source_relationship",
            role="triage_source_relationship",
            rationale="Persisted by finalizer update_source_finding_relations.",
        )
        if selection_error:
            return selection_error

        previous_finding = deepcopy(finding)
        finding["source_relations"] = normalized_relations
        finding["updated_at"] = workspace_api._now()
        self._recompute_all_set_evidence_ids(current)
        from app.generation_v2.source_triage_workspace_tools import (
            carry_finalizer_finding_review_forward,
        )

        carry_finalizer_finding_review_forward(
            current,
            previous_finding=previous_finding,
        )
        await workspace_api.save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=current,
        )
        return {
            "accepted": True,
            "finding_id": finding_id,
            "changed": True,
            "relation_count": len(normalized_relations),
            "stale_outcome_question_ids": [],
        }


__all__ = ["SourceTriageWorkspaceFindingHandlersMixin"]
