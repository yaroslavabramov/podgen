from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.source_triage_grounding_audit_state import (
    GROUNDING_AUDITS_FIELD,
    evidence_requiring_full_finalizer_read,
    finding_grounding_signature,
    grounding_audit_payload_error,
)
from app.generation_v2.source_triage_source_analysis_contract import (
    reconcile_source_assessment_findings,
)
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_workspace_state import invalidate_coverage_syntheses


class SourceTriageWorkspaceGroundingAuditHandlersMixin:
    async def audit_source_finding_tool(self, args: dict[str, Any]) -> dict[str, Any]:
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
        verdict = str(args.get("verdict") or "").strip()
        if verdict not in {"supported", "narrowed", "removed"}:
            return {"accepted": False, "error": "invalid grounding audit verdict"}
        rationale = str(args.get("rationale") or "").strip()
        rationale_error = semantic_field_error(
            {"finding_support_explanation": rationale},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["finding_support_explanation"],
        )
        if rationale_error:
            return rationale_error
        if not rationale:
            return {"accepted": False, "error": "grounding audit rationale is required"}
        shape_error = grounding_audit_payload_error(args, verdict=verdict)
        if shape_error:
            return shape_error

        revised_evidence_ids = workspace_api._dedupe(
            [str(value) for value in args.get("revised_evidence_ids") or [] if str(value)]
        )
        if revised_evidence_ids:
            evidence_error = await self._validate_revised_finding_evidence(
                current,
                finding,
                evidence_ids=revised_evidence_ids,
            )
            if evidence_error:
                return evidence_error
        audit_candidate = deepcopy(finding)
        if revised_evidence_ids:
            audit_candidate["evidence_ids"] = revised_evidence_ids
            audit_candidate["evidence_previews"] = [
                item
                for item in finding.get("evidence_previews") or []
                if isinstance(item, dict)
                and str(item.get("evidence_id") or "") in set(revised_evidence_ids)
            ]
        unread_evidence_ids = evidence_requiring_full_finalizer_read(current, audit_candidate)
        if unread_evidence_ids:
            return {
                "accepted": False,
                "error": "finding evidence must be read in full before its grounding audit",
                "evidence_ids": unread_evidence_ids,
                "next_action": (
                    "Call read_evidence for each evidence_id and follow next_page_ref until "
                    "exact_read_complete=true, then retry."
                ),
            }

        previous_finding = deepcopy(finding)
        affected_question_ids = {
            str(link.get("question_id") or "")
            for link in finding.get("question_links") or []
            if isinstance(link, dict) and str(link.get("question_id") or "")
        }
        changed = False
        if verdict == "narrowed":
            validation_error = await self._apply_narrowed_finding(
                current,
                finding,
                args,
                revised_evidence_ids=revised_evidence_ids,
            )
            if validation_error:
                return validation_error
            changed = True
        elif verdict == "removed":
            removal_error = self._remove_unsupported_finding(current, finding, args)
            if removal_error:
                return removal_error
            changed = True
        elif revised_evidence_ids and revised_evidence_ids != [
            str(value) for value in finding.get("evidence_ids") or []
        ]:
            await self._replace_finding_evidence(
                current,
                finding,
                evidence_ids=revised_evidence_ids,
            )
            changed = True

        if verdict != "removed":
            audits = dict(current.get(GROUNDING_AUDITS_FIELD) or {})
            audits[finding_id] = {
                "finding_id": finding_id,
                "signature": finding_grounding_signature(finding),
                "verdict": verdict,
                "rationale": rationale,
                "updated_at": workspace_api._now(),
            }
            current[GROUNDING_AUDITS_FIELD] = audits
        else:
            current.setdefault("source_finding_grounding_audit_history", []).append(
                {
                    "finding_id": finding_id,
                    "signature": finding_grounding_signature(previous_finding),
                    "verdict": verdict,
                    "rationale": rationale,
                    "updated_at": workspace_api._now(),
                }
            )

        if changed:
            for outcome in current.get("coverage_outcomes") or []:
                if (
                    isinstance(outcome, dict)
                    and str(outcome.get("question_id") or "") in affected_question_ids
                ):
                    outcome["is_stale"] = True
            self._recompute_all_set_evidence_ids(current)
            invalidate_coverage_syntheses(current)
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
            "verdict": verdict,
            "changed": changed,
        }

    async def _apply_narrowed_finding(
        self,
        current: dict[str, Any],
        finding: dict[str, Any],
        args: dict[str, Any],
        *,
        revised_evidence_ids: list[str],
    ) -> dict[str, Any] | None:
        statement = str(args.get("revised_statement") or "").strip()
        support_explanation = str(args.get("revised_support_explanation") or "").strip()
        fields_error = semantic_field_error(
            {
                "finding_statement": statement,
                "finding_support_explanation": support_explanation,
            },
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["finding_statement", "finding_support_explanation"],
        )
        if fields_error:
            return fields_error
        if not statement or not support_explanation:
            return {
                "accepted": False,
                "error": "narrowed finding requires revised_statement and revised_support_explanation",
            }
        raw_scope = args.get("revised_scope")
        if not isinstance(raw_scope, dict):
            return {"accepted": False, "error": "revised_scope must be an object"}
        scope = {
            "actor": str(raw_scope.get("actor") or "").strip(),
            "period": str(raw_scope.get("period") or "").strip(),
            "geography": str(raw_scope.get("geography") or "").strip(),
            "qualifiers": [str(value).strip() for value in raw_scope.get("qualifiers") or [] if str(value).strip()],
        }
        scope_error = semantic_field_error(
            {key: value for key, value in scope.items() if key != "qualifiers"},
            {key: TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"] for key in ("actor", "period", "geography")},
            ["actor", "period", "geography"],
        )
        if scope_error:
            return scope_error
        qualifiers_error = semantic_list_error(
            field="finding_scope_qualifiers",
            values=scope["qualifiers"],
            max_items=TRIAGE_LIMITATION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
        )
        if qualifiers_error:
            return qualifiers_error
        limitations = [str(value).strip() for value in args.get("revised_limitations") or [] if str(value).strip()]
        limitations_error = semantic_list_error(
            field="finding_limitations",
            values=limitations,
            max_items=TRIAGE_LIMITATION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
        )
        if limitations_error:
            return limitations_error
        finding["statement"] = statement
        finding["support_explanation"] = support_explanation
        finding["scope"] = scope
        finding["limitations"] = limitations
        finding["semantic_revision"] = int(current.get("triage_semantic_revision") or 0) + 1
        finding["updated_at"] = workspace_api._now()
        await self._replace_finding_evidence(
            current,
            finding,
            evidence_ids=revised_evidence_ids,
        )
        return None

    async def _replace_finding_evidence(
        self,
        current: dict[str, Any],
        finding: dict[str, Any],
        *,
        evidence_ids: list[str],
    ) -> None:
        finding["evidence_ids"] = list(evidence_ids)
        finding["semantic_revision"] = int(current.get("triage_semantic_revision") or 0) + 1
        finding["updated_at"] = workspace_api._now()
        if self.evidence_toolset is not None:
            from app.generation_v2.source_triage_source_analysis_contract import attach_evidence_previews

            await attach_evidence_previews([finding], evidence_toolset=self.evidence_toolset)
        else:
            finding["evidence_previews"] = [
                item
                for item in finding.get("evidence_previews") or []
                if isinstance(item, dict)
                and str(item.get("evidence_id") or "") in set(evidence_ids)
            ]
        source_id = str(finding.get("source_id") or "")
        set_id = str(finding.get("set_id") or "")
        for assessment in current.get("source_assessments") or []:
            if (
                isinstance(assessment, dict)
                and str(assessment.get("source_id") or "") == source_id
                and str(assessment.get("set_id") or "") == set_id
            ):
                assessment["evidence_ids"] = workspace_api._dedupe(
                    [*(assessment.get("evidence_ids") or []), *evidence_ids]
                )
                assessment["updated_at"] = workspace_api._now()

    async def _validate_revised_finding_evidence(
        self,
        current: dict[str, Any],
        finding: dict[str, Any],
        *,
        evidence_ids: list[str],
    ) -> dict[str, Any] | None:
        set_id = str(finding.get("set_id") or "")
        source_set = workspace_api._workspace_set(current, set_id)
        if source_set is None:
            return {
                "accepted": False,
                "error": "Finding owner set no longer exists",
                "set_id": set_id,
            }
        validation_error = await self._validate_set_evidence_ids(
            source_set=source_set,
            evidence_ids=evidence_ids,
            required_source_id=str(finding.get("source_id") or ""),
            selection_purpose="coverage_outcome",
            selection_role="triage_coverage_support",
        )
        if validation_error:
            return validation_error
        blocked_evidence_ids = await self._blocked_shell_evidence_ids(evidence_ids)
        if blocked_evidence_ids:
            return {
                "accepted": False,
                "error": "Shell/error/access-page evidence cannot support a substantive finding",
                "invalid_evidence_ids": blocked_evidence_ids,
            }
        return await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose="coverage_outcome",
            role="triage_coverage_support",
            rationale="Selected by finalizer grounding audit.",
        )

    def _remove_unsupported_finding(
        self,
        current: dict[str, Any],
        finding: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        finding_id = str(finding.get("finding_id") or "")
        source_id = str(finding.get("source_id") or "")
        set_id = str(finding.get("set_id") or "")
        remaining = [
            item
            for item in current.get("source_findings") or []
            if isinstance(item, dict)
            and not (
                str(item.get("finding_id") or "") == finding_id
                and str(item.get("source_id") or "") == source_id
            )
        ]
        remaining_for_source = [
            item
            for item in remaining
            if str(item.get("source_id") or "") == source_id
            and str(item.get("set_id") or "") == set_id
        ]
        no_findings_reason = str(args.get("source_no_findings_reason") or "").strip()
        no_findings_error = semantic_field_error(
            {"no_findings_reason": no_findings_reason},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["no_findings_reason"],
        )
        if no_findings_error:
            return no_findings_error
        if remaining_for_source and no_findings_reason:
            return {
                "accepted": False,
                "error": "source_no_findings_reason must be empty while other findings remain",
            }
        if not remaining_for_source and not no_findings_reason:
            return {
                "accepted": False,
                "error": "Removing the source's last finding requires source_no_findings_reason",
            }
        current["source_findings"] = remaining
        audits = dict(current.get(GROUNDING_AUDITS_FIELD) or {})
        audits.pop(finding_id, None)
        current[GROUNDING_AUDITS_FIELD] = audits
        for assessment in current.get("source_assessments") or []:
            if (
                isinstance(assessment, dict)
                and str(assessment.get("source_id") or "") == source_id
                and str(assessment.get("set_id") or "") == set_id
            ):
                reconcile_source_assessment_findings(
                    assessment,
                    findings=remaining_for_source,
                )
                if not remaining_for_source:
                    assessment["no_findings_reason"] = no_findings_reason
                assessment["updated_at"] = workspace_api._now()
        source_set = workspace_api._workspace_set(current, set_id)
        if source_set is not None:
            self._recompute_source_set_question_links(current, source_set)
            source_set["updated_at"] = workspace_api._now()
            workspace_api._replace_workspace_set(current, source_set)
        return None
__all__ = [
    "SourceTriageWorkspaceGroundingAuditHandlersMixin",
]
