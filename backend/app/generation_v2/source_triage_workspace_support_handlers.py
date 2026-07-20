from __future__ import annotations

from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.source_triage_workspace_handler_helpers import (
    finding_supports_question as _finding_supports_question,
)
from app.generation_v2.tools.evidence_utils import _unsupported_model_read_selected_as_support


class SourceTriageWorkspaceSupportHandlersMixin:
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
        not_exact_read: list[str] = []
        allowed_refs = set(source_set.get("source_refs") or [])
        exact_read_checker = getattr(
            self.evidence_toolset,
            "_evidence_has_current_exact_read",
            None,
        )
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
                continue
            if callable(exact_read_checker) and not exact_read_checker(evidence):
                not_exact_read.append(evidence_id)
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
        if not_exact_read:
            return {
                "accepted": False,
                "error": "Semantic triage evidence must have a current substantive exact read",
                "not_exact_read_evidence_ids": not_exact_read,
                "next_action": (
                    "Call read_evidence or read_source_window for each listed evidence item in the current run, "
                    "then retry with the returned exact-read evidence_ids. Search/skim previews are not support."
                ),
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
            stored_cross_ids = {str(item) for item in source_set.get("cross_question_ids") or []}
            planned_question_ids = workspace_api._dedupe(
                [str(item) for item in source_set.get("question_ids") or [] if str(item) not in stored_cross_ids]
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
            question_id for question_id in assessment_question_ids if question_id not in set(planned_question_ids)
        ]
        previous_question_ids = {str(item) for item in source_set.get("question_ids") or []}
        source_set["planned_question_ids"] = planned_question_ids
        source_set["cross_question_ids"] = cross_question_ids
        source_set["question_ids"] = workspace_api._dedupe([*planned_question_ids, *cross_question_ids])
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
    def _record_pending_discovered_question_attempt(
        *,
        source_set: dict[str, Any],
        question_id: str,
        finding_ids: list[str],
        findings_by_id: dict[str, dict[str, Any]],
        requirement_ids: list[str],
    ) -> None:
        existing = next(
            (
                item
                for item in source_set.get("pending_discovered_question_attempts") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            None,
        )
        combined_finding_ids = workspace_api._dedupe(
            [
                *([str(item) for item in existing.get("finding_ids") or []] if isinstance(existing, dict) else []),
                *finding_ids,
            ]
        )
        source_refs = workspace_api._dedupe(
            [
                str(findings_by_id[finding_id].get("source_ref") or "")
                for finding_id in combined_finding_ids
                if finding_id in findings_by_id and str(findings_by_id[finding_id].get("source_ref") or "")
            ]
        )
        attempt = {
            "question_id": question_id,
            "requirement_ids": workspace_api._dedupe(
                [
                    *(
                        [str(item) for item in existing.get("requirement_ids") or []]
                        if isinstance(existing, dict)
                        else []
                    ),
                    *requirement_ids,
                ]
            ),
            "finding_ids": combined_finding_ids,
            "source_refs": source_refs,
            "reviewed_source_refs": (
                [str(item) for item in existing.get("reviewed_source_refs") or []] if isinstance(existing, dict) else []
            ),
            "updated_at": workspace_api._now(),
        }
        source_set["pending_discovered_question_attempts"] = [
            item
            for item in source_set.get("pending_discovered_question_attempts") or []
            if not isinstance(item, dict) or str(item.get("question_id") or "") != question_id
        ]
        source_set["pending_discovered_question_attempts"].append(attempt)

    @staticmethod
    def _reconcile_pending_discovered_question_attempts(
        *,
        workspace: dict[str, Any],
        source_set: dict[str, Any],
        reviewed_source_ref: str | None,
    ) -> None:
        set_id = str(source_set.get("set_id") or "")
        findings = [
            item
            for item in workspace.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ]
        pending: list[dict[str, Any]] = []
        for raw_attempt in source_set.get("pending_discovered_question_attempts") or []:
            if not isinstance(raw_attempt, dict):
                continue
            attempt = dict(raw_attempt)
            question_id = str(attempt.get("question_id") or "")
            attempted_finding_ids = {str(item) for item in attempt.get("finding_ids") or []}
            linked = any(
                str(finding.get("finding_id") or "") in attempted_finding_ids
                and _finding_supports_question(finding, question_id)
                for finding in findings
            )
            if linked:
                # The durable finding link now makes this ordinary discovered
                # set work; question_ids/outcome completeness enforce the rest.
                continue
            reviewed_refs = {str(item) for item in attempt.get("reviewed_source_refs") or []}
            source_refs = {str(item) for item in attempt.get("source_refs") or []}
            if reviewed_source_ref and reviewed_source_ref in source_refs:
                reviewed_refs.add(reviewed_source_ref)
            if source_refs and source_refs.issubset(reviewed_refs):
                # Every owner source was explicitly reviewed and accepted again
                # after the rejected attempt without the proposed link. A
                # semantic no-op is valid confirmation; no invented change is
                # required to clear an erroneous attempted link.
                continue
            attempt["reviewed_source_refs"] = sorted(reviewed_refs)
            pending.append(attempt)
        source_set["pending_discovered_question_attempts"] = pending

    @staticmethod
    def _recompute_all_set_evidence_ids(workspace: dict[str, Any]) -> None:
        evidence_by_set: dict[str, list[str]] = {}
        for field in ("source_assessments", "source_findings", "coverage_outcomes"):
            for item in workspace.get(field) or []:
                if not isinstance(item, dict):
                    continue
                set_id = str(item.get("set_id") or "")
                if set_id:
                    evidence_by_set.setdefault(set_id, []).extend(
                        str(value) for value in item.get("evidence_ids") or []
                    )
                    if field == "source_findings":
                        evidence_by_set[set_id].extend(
                            str(value)
                            for relation in item.get("source_relations") or []
                            if isinstance(relation, dict)
                            for value in relation.get("evidence_ids") or []
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


__all__ = ["SourceTriageWorkspaceSupportHandlersMixin"]
