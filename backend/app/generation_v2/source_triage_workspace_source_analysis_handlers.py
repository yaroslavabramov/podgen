from __future__ import annotations

import hashlib
import json
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.model_optional_fields import optional_model_text
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.runtime_contracts import AgentOperationalStall
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_EDITORIAL_FUNCTION_LIMIT,
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_workspace_handler_helpers import (
    confidence as _confidence,
    pending_set_work as _pending_set_work,
    semantic_json as _semantic_json,
)
from app.generation_v2.source_triage_workspace_state import invalidate_coverage_syntheses
from app.generation_v2.source_triage_workspace_coverage_hint_handlers import (
    retire_coverage_hints_for_source_analysis,
)
from app.generation_v2.source_triage_source_analysis_contract import (
    attach_evidence_previews,
    validate_reading_basis,
)
from app.generation_v2.source_triage_read_checkpointing import (
    clear_pending_comparison_read,
    clear_pending_exact_read,
    source_refs_with_reads_newer_than_analysis,
)
from app.generation_v2.source_triage_source_analysis_state import (
    has_delivered_source_eof,
    has_delivered_source_read,
    has_source_representation_failure,
    source_read_complete_from_backend,
)
from app.generation_v2.source_triage_passage_analysis import (
    normalize_passage_analysis_args,
    preserve_downstream_fields_for_unchanged_findings,
    uses_passage_analysis_contract,
)
from app.generation_v2.source_triage_question_references import (
    possible_question_ids_error,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.ingestion.web_quality import assess_web_fetch_health


class SourceTriageWorkspaceSourceAnalysisHandlersMixin:
    async def upsert_source_assessment_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        if "status" in args and "read_state" in args:
            return await self._upsert_source_analysis_record(args)
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

        v3_analysis = uses_passage_analysis_contract(args)
        if v3_analysis:
            normalized_args, materialization_error = await normalize_passage_analysis_args(
                self,
                workspace=current,
                source_set=source_set,
                source_ref=source_ref,
                args=args,
            )
            if materialization_error:
                return materialization_error
            args = normalized_args

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
        duplicate_of_source_ref = optional_model_text(args.get("duplicate_of_source_ref"))
        verified_duplicate_ref = optional_model_text(args.get("verified_exact_duplicate_of_source_ref"))
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
        if (
            values["representation_status"]
            in {
                "partial",
                "not_target_content",
                "unreadable",
            }
            and not reason_fields["representation_reason"]
        ):
            return {
                "accepted": False,
                "error": ("partial/not_target_content/unreadable representation requires representation_reason"),
            }
        if values["disposition"] in {"weak", "unusable", "deferred"} and not reason_fields["disposition_reason"]:
            return {
                "accepted": False,
                "error": "weak/unusable/deferred disposition requires disposition_reason",
            }
        if (
            values["representation_status"]
            in {
                "not_target_content",
                "unreadable",
            }
            and values["disposition"] != "deferred"
        ):
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
                    "disposition=deferred requires a checked partial, not_target_content, or unreadable representation"
                ),
            }
        if (
            verified_duplicate_ref
            and values["uniqueness"] == "duplicate"
            and values["representation_status"] in {"not_target_content", "unreadable"}
        ):
            return {
                "accepted": False,
                "error": "verified_duplicate_must_be_saved_as_readable_target_content",
                "source_ref": source_ref,
                "duplicate_of_source_ref": verified_duplicate_ref,
                "invalid_fields": ["representation_status", "disposition"],
                "corrected_fields": {
                    "representation_status": "readable",
                    "representation_reason": "",
                    "uniqueness": "duplicate",
                    "duplicate_of_source_ref": verified_duplicate_ref,
                    "disposition": "background",
                    "findings": [],
                },
                "next_action": (
                    "Retry save_source_analysis for this same source as readable target content with "
                    "uniqueness=duplicate and duplicate_of_source_ref copied exactly. Do not mark a verified "
                    "exact duplicate as not_target_content or unreadable."
                ),
            }
        if values["representation_status"] in {"not_target_content", "unreadable"}:
            if values["processing_status"] != "resolved":
                return {
                    "accepted": False,
                    "error": (
                        "A checked unavailable representation is a completed diagnosis and requires "
                        "processing_status=resolved"
                    ),
                }
            non_unknown_axes = [
                field for field in ("relevance", "authority", "uniqueness") if values[field] != "unknown"
            ]
            if non_unknown_axes:
                return {
                    "accepted": False,
                    "error": (
                        "Unavailable target content cannot establish relevance, authority, or uniqueness; "
                        "all three judgments must be unknown"
                    ),
                    "invalid_fields": non_unknown_axes,
                    "corrected_fields": {
                        "relevance": "unknown",
                        "authority": "unknown",
                        "uniqueness": "unknown",
                        "duplicate_of_source_ref": "",
                    },
                    "allowed_next_actions": [
                        {
                            "tool": "read_source",
                            "arguments": {
                                "source_ref": source_ref,
                                "cursor": "",
                                "max_chars": 20_000,
                            },
                            "when": "Use this if the source is readable and you need to establish content or duplicate status.",
                        },
                        {
                            "tool": "save_source_analysis",
                            "arguments": {
                                "source_ref": source_ref,
                                "representation_status": values["representation_status"],
                                "relevance": "unknown",
                                "authority": "unknown",
                                "uniqueness": "unknown",
                                "duplicate_of_source_ref": "",
                            },
                            "when": "Use this only if the checked representation is genuinely unavailable or not target content.",
                        },
                    ],
                    "next_action": (
                        "If you can read or compare this source, do that before judging relevance/authority/"
                        "uniqueness. If the target content is genuinely unavailable, retry with relevance, "
                        "authority, and uniqueness all unknown and duplicate_of_source_ref empty."
                    ),
                }
            asserted_reason_fields = [
                field for field in ("relevance_reason", "authority_reason", "uniqueness_reason") if reason_fields[field]
            ]
            if asserted_reason_fields:
                return {
                    "accepted": False,
                    "error": (
                        "Unavailable target content leaves source judgments unknown; their reason fields must be empty"
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

        analysis_mode = "findings" in args
        findings: list[dict[str, Any]] = []
        finding_question_ids: list[str] = []
        finding_evidence_ids: list[str] = []
        if analysis_mode:
            enhanced_analysis_contract = (
                "reading_basis" in args
                or any(
                    isinstance(item, dict) and "support_explanation" in item
                    for item in args.get("findings") or []
                )
            )
            findings, finding_question_ids, finding_evidence_ids, finding_error = await self._validate_source_findings(
                current=current,
                source_set=source_set,
                source_ref=source_ref,
                source_id=source_id,
                assessment_id=f"assessment:{source_id}",
                raw_findings=args.get("findings"),
                require_support_explanation=enhanced_analysis_contract,
            )
            if finding_error:
                return finding_error
            if enhanced_analysis_contract:
                reading_basis, reading_basis_error = validate_reading_basis(
                    workspace=current,
                    source_set=source_set,
                    source_ref=source_ref,
                    representation_status=values["representation_status"],
                    raw_basis=args.get("reading_basis"),
                    verified_exact_duplicate_of_source_ref=str(
                        args.get("verified_exact_duplicate_of_source_ref") or ""
                    ),
                )
                if reading_basis_error:
                    return reading_basis_error
            else:
                # Historical internal callers predate the richer model contract.
                # Production tool schemas always provide both records.
                reading_basis = {}
            # Finding links are the authoritative per-source/question relation.
            # Do not persist an O(sources × questions) matrix of boilerplate.
            question_dispositions = []
            if self.evidence_toolset is not None:
                await attach_evidence_previews(
                    findings,
                    evidence_toolset=self.evidence_toolset,
                    preview_chars=3_000 if v3_analysis else 900,
                )
        else:
            reading_basis = dict(args.get("reading_basis") or {})
            question_dispositions = list(args.get("question_dispositions") or [])
        relation_evidence_ids = public_api._dedupe(
            [
                str(evidence_id)
                for finding in findings
                for relation in finding.get("source_relations") or []
                if isinstance(relation, dict)
                for evidence_id in relation.get("evidence_ids") or []
                if str(evidence_id)
            ]
        )
        question_ids = public_api._dedupe(
            finding_question_ids
            if analysis_mode
            else [str(item) for item in args.get("coverage_question_ids") or [] if str(item).strip()]
        )
        if analysis_mode:
            values["relevance"] = _derived_relevance(
                representation_status=values["representation_status"],
                findings=findings,
                question_ids=question_ids,
            )
            if values["disposition"] == "background" and values["relevance"] == "supporting":
                values["disposition"] = "supporting"
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
        if values["representation_status"] in {"not_target_content", "unreadable"} and (
            question_ids or editorial_functions
        ):
            return {
                "accepted": False,
                "error": ("Unavailable target content cannot establish coverage-question or editorial-function links"),
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
        no_findings_reason = str(args.get("no_findings_reason") or "").strip()
        no_findings_error = semantic_field_error(
            {"no_findings_reason": no_findings_reason},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["no_findings_reason"],
        )
        if no_findings_error:
            return no_findings_error
        source_summary = str(args.get("source_summary") or "").strip()
        source_summary_error = semantic_field_error(
            {"source_summary": source_summary},
            {"source_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["findings_summary"]},
            ["source_summary"],
        )
        if source_summary_error:
            return source_summary_error
        if analysis_mode and not findings and not no_findings_reason:
            return {
                "accepted": False,
                "error": "An empty findings list requires no_findings_reason after actual reading",
                "next_action": "Read the source, then explain why it established no substantive finding.",
            }
        if analysis_mode and findings and no_findings_reason:
            return {
                "accepted": False,
                "error": "no_findings_reason must be empty when substantive findings are saved",
            }
        if analysis_mode and values["representation_status"] in {"not_target_content", "unreadable"} and findings:
            return {
                "accepted": False,
                "error": "A non-target or unreadable representation cannot establish source findings",
                "next_action": "Save an empty findings list and explain the representation problem.",
            }
        evidence_ids = public_api._dedupe(
            [
                *[str(item) for item in args.get("evidence_ids") or [] if str(item).strip()],
                *finding_evidence_ids,
            ]
        )
        verified_exact_duplicate = bool(
            verified_duplicate_ref
            and values["uniqueness"] == "duplicate"
            and duplicate_of_source_ref == verified_duplicate_ref
        )
        requires_grounded_evidence = values["representation_status"] == "not_target_content" or (
            values["processing_status"] == "resolved"
            and values["representation_status"] in {"readable", "partial"}
            and not verified_exact_duplicate
        )
        if requires_grounded_evidence and not evidence_ids:
            return {
                "accepted": False,
                "error": (
                    "A resolved readable/partial/not_target_content source assessment requires grounded evidence "
                    "from that source"
                ),
            }
        supports_content_judgment = values["processing_status"] == "resolved" and values["representation_status"] in {
            "readable",
            "partial",
        }
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
        relation_selection_error = await self._select_validated_evidence_ids(
            evidence_ids=relation_evidence_ids,
            purpose="source_relationship",
            role="triage_source_relationship",
            rationale="Persisted as source-relation evidence in save_source_analysis.",
        )
        if relation_selection_error:
            return relation_selection_error

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
            "duplicate_of_source_ref": duplicate_of_source_ref,
            "disposition_reason": reason_fields["disposition_reason"],
            "coverage_question_ids": question_ids,
            "editorial_functions": editorial_functions,
            "evidence_ids": evidence_ids,
            "finding_ids": [str(item.get("finding_id") or "") for item in findings],
            "reading_basis": reading_basis,
            "question_dispositions": question_dispositions,
            "source_summary": source_summary,
            "no_findings_reason": no_findings_reason,
            "limitations": limitations,
            "confidence": _confidence(args.get("confidence")),
            "updated_at": public_api._now(),
        }
        old_assessment = next(
            (
                item
                for item in current.get("source_assessments") or []
                if isinstance(item, dict) and str(item.get("source_id") or "") == source_id
            ),
            None,
        )
        old_findings = [
            item
            for item in current.get("source_findings") or []
            if isinstance(item, dict)
            and str(item.get("source_id") or "") == source_id
            and str(item.get("set_id") or "") == set_id
        ]
        if v3_analysis:
            preserve_downstream_fields_for_unchanged_findings(
                findings,
                previous_findings=old_findings,
            )
        analysis_changed = (
            not analysis_mode
            or _semantic_json(old_assessment) != _semantic_json(assessment)
            or _semantic_json(old_findings) != _semantic_json(findings)
        )
        if analysis_mode and analysis_changed:
            # A later source-analysis write changes the finalizer's authoritative
            # review stream. Make it read the current questions/findings again.
            current.pop("source_triage_finalizer_reads", None)
            grounding_audits = dict(current.get("source_finding_grounding_audits") or {})
            for finding_id in {
                str(item.get("finding_id") or "")
                for item in [*old_findings, *findings]
                if isinstance(item, dict) and str(item.get("finding_id") or "")
            }:
                grounding_audits.pop(finding_id, None)
            current["source_finding_grounding_audits"] = grounding_audits
            next_revision = int(current.get("triage_semantic_revision") or 0) + 1
            current["triage_semantic_revision"] = next_revision
            for finding in findings:
                finding["semantic_revision"] = next_revision
            affected_question_ids = {
                *finding_question_ids,
                *[
                    str(link.get("question_id") or "")
                    for finding in old_findings
                    for link in finding.get("question_links") or []
                    if isinstance(link, dict)
                ],
            } - {""}
            old_representation_status = str((old_assessment or {}).get("representation_status") or "unchecked")
            if old_assessment is not None and old_representation_status != values["representation_status"]:
                # A representation transition can invalidate a previous deferral even when
                # neither revision has a substantive finding to link to a question.
                affected_question_ids.update(
                    str(question_id) for question_id in source_set.get("question_ids") or [] if str(question_id)
                )
            for outcome in current.get("coverage_outcomes") or []:
                if (
                    isinstance(outcome, dict)
                    and str(outcome.get("set_id") or "") == set_id
                    and str(outcome.get("question_id") or "") in affected_question_ids
                ):
                    outcome["is_stale"] = True
            current["source_findings"] = [
                item
                for item in current.get("source_findings") or []
                if not (
                    isinstance(item, dict)
                    and str(item.get("source_id") or "") == source_id
                    and str(item.get("set_id") or "") == set_id
                )
            ]
            current["source_findings"].extend(findings)
            retired_coverage_hint_ids = retire_coverage_hints_for_source_analysis(
                current,
                set_id=set_id,
                source_ref=source_ref,
                reason="source_analysis_changed",
            )
        elif analysis_mode:
            # Preserve existing revision/timestamps on a semantic no-op.
            findings = old_findings
            assessment["finding_ids"] = [str(item.get("finding_id") or "") for item in old_findings]
            retired_coverage_hint_ids = []
        else:
            retired_coverage_hint_ids = []
        current["source_assessments"] = [
            item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_id") or "") != source_id
        ]
        current["source_assessments"].append(assessment)
        if v3_analysis:
            assigned_question_ids = public_api._dedupe(
                [
                    str(value)
                    for value in source_set.get("assigned_question_ids", source_set.get("question_ids") or [])
                    if str(value)
                ]
            )
            source_set["assigned_question_ids"] = assigned_question_ids
            source_set["planned_question_ids"] = assigned_question_ids
            source_set["question_ids"] = assigned_question_ids
            source_set["cross_question_ids"] = public_api._dedupe(
                [
                    *(source_set.get("cross_question_ids") or []),
                    *(question_id for question_id in question_ids if question_id not in set(assigned_question_ids)),
                ]
            )
        clear_pending_exact_read(source_set, source_ref=source_ref)
        clear_pending_comparison_read(source_set, source_ref=source_ref)
        self._reconcile_pending_discovered_question_attempts(
            workspace=current,
            source_set=source_set,
            reviewed_source_ref=source_ref if analysis_mode else None,
        )
        self._recompute_source_set_question_links(current, source_set)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        if analysis_changed:
            invalidate_coverage_syntheses(current)
        if analysis_mode and source_ref in source_refs_with_reads_newer_than_analysis(
            current,
            set_id=set_id,
        ):
            raise AgentOperationalStall(
                "source analysis invariant failed: saved read_state_signature is not current "
                f"for set_id={set_id!r}, source_ref={source_ref!r}"
            )
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        pending_work = _pending_set_work(current, source_set)
        return {
            "accepted": True,
            "source_ref": source_ref,
            "processing_status": values["processing_status"],
            "representation_status": values["representation_status"],
            "disposition": values["disposition"],
            "finding_ids": list(assessment.get("finding_ids") or []),
            "analysis_changed": analysis_changed,
            "retired_coverage_hint_ids": retired_coverage_hint_ids,
            "next_action": (
                "Continue source mapping. Add coverage hints for materially relevant questions, or read the next "
                "unresolved source."
            ),
            **pending_work,
        }

    async def _upsert_source_analysis_record(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        source_ref = str(args.get("source_ref") or "").strip()
        if source_ref not in {str(item) for item in source_set.get("source_refs") or []}:
            return {
                "accepted": False,
                "error": "source_ref must belong to the current source set",
                "source_ref": source_ref,
            }
        source_id = resolve_source_ref(source_ref, build_source_ref_context(self.inventory))
        if not source_id:
            return {"accepted": False, "error": "Unknown source_ref", "source_ref": source_ref}

        status = str(args.get("status") or "").strip()
        if status not in {"usable", "deferred", "not_target_content"}:
            return {
                "accepted": False,
                "error": "status must be usable, deferred, or not_target_content",
                "source_ref": source_ref,
            }
        status_reason = str(args.get("status_reason") or "").strip()
        summary = str(args.get("summary") or "").strip()
        if status != "usable" and not status_reason:
            return {"accepted": False, "error": "deferred/not_target_content requires status_reason"}
        if status == "usable" and not summary:
            return {"accepted": False, "error": "usable source analysis requires summary"}
        duplicate_of_source_ref = str(args.get("duplicate_of_source_ref") or "").strip()
        duplicate_allowed = bool(
            duplicate_of_source_ref
            and _backend_duplicate_allowed(
                current,
                source_ref=source_ref,
                duplicate_of_source_ref=duplicate_of_source_ref,
            )
        )
        if duplicate_of_source_ref and not duplicate_allowed:
            return {
                "accepted": False,
                "error": "duplicate_of_source_ref must come from backend exact-duplicate metadata",
                "source_ref": source_ref,
                "duplicate_of_source_ref": duplicate_of_source_ref,
            }
        representation_failure = has_source_representation_failure(
            self.inventory,
            source_id,
            workspace=current,
            set_id=set_id,
            source_ref=source_ref,
        )
        source_limitations = [str(item).strip() for item in args.get("source_limitations") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="source_limitations",
            values=source_limitations,
            max_items=TRIAGE_LIMITATION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
        )
        if list_error:
            return list_error
        field_error = semantic_field_error(
            {
                "status_reason": status_reason,
                "source_summary": summary,
            },
            {
                "status_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "source_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["findings_summary"],
            },
            ["status_reason", "source_summary"],
        )
        if field_error:
            return field_error

        read_state = args.get("read_state") if isinstance(args.get("read_state"), dict) else {}
        model_claimed_complete = bool(read_state.get("complete"))
        backend_read_complete = source_read_complete_from_backend(
            current,
            set_id=set_id,
            source_ref=source_ref,
            exact_duplicate=duplicate_allowed,
            representation_failure=representation_failure and status == "deferred",
        )
        if model_claimed_complete and not backend_read_complete:
            return {
                "accepted": False,
                "error": "read_state.complete requires delivered read_source EOF",
                "source_ref": source_ref,
                "next_action": "Continue read_source to eof, or set complete=false with unread_regions and stopping_reason.",
            }
        read_complete = model_claimed_complete
        unread_regions = [str(item).strip() for item in read_state.get("unread_regions") or [] if str(item).strip()]
        stopping_reason = str(read_state.get("stopping_reason") or "").strip()
        if not read_complete and (not unread_regions or not stopping_reason):
            return {
                "accepted": False,
                "error": "read_state.complete=false requires unread_regions and stopping_reason",
                "source_ref": source_ref,
            }
        if status == "not_target_content" and not has_delivered_source_read(
            current,
            set_id=set_id,
            source_ref=source_ref,
        ):
            return {
                "accepted": False,
                "error": "not_target_content requires a delivered read_source receipt",
                "source_ref": source_ref,
                "next_action": "Read enough target text with read_source, then save not_target_content with boundaries.",
            }
        if status == "deferred" and not (
            has_delivered_source_read(current, set_id=set_id, source_ref=source_ref)
            or representation_failure
        ):
            return {
                "accepted": False,
                "error": "deferred requires delivered read_source evidence or backend representation failure",
                "source_ref": source_ref,
                "next_action": "Use read_source if text exists; defer only when backend has no usable representation.",
            }

        known_leaf_question_ids = _leaf_question_ids(current)
        raw_findings = args.get("findings") or []
        if not isinstance(raw_findings, list):
            return {"accepted": False, "error": "findings must be an array"}
        if status != "usable" and raw_findings:
            return {
                "accepted": False,
                "error": "deferred/not_target_content source analysis cannot contain findings",
                "source_ref": source_ref,
            }
        findings: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        for index, raw_finding in enumerate(raw_findings):
            if not isinstance(raw_finding, dict):
                return {"accepted": False, "error": "Each finding must be an object", "finding_index": index}
            statement = str(raw_finding.get("statement") or "").strip()
            if not statement:
                return {
                    "accepted": False,
                    "error": "Each finding requires a statement",
                    "finding_index": index,
                }
            field_error = semantic_field_error(
                {"finding_statement": statement},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_statement"],
            )
            if field_error:
                return field_error
            finding_evidence_ids = public_api._dedupe(
                [str(item) for item in raw_finding.get("evidence_ids") or [] if str(item).strip()]
            )
            finding_passage_refs = public_api._dedupe(
                [str(item) for item in raw_finding.get("passage_refs") or [] if str(item).strip()]
            )
            if finding_passage_refs:
                if self.evidence_toolset is None or not hasattr(
                    self.evidence_toolset,
                    "materialize_source_passages",
                ):
                    return {
                        "accepted": False,
                        "error": "source passage materialization is unavailable",
                        "finding_index": index,
                        "next_action": "Retry after read_source returned delivered passage_refs in this source mapping run.",
                    }
                passage_result = await self.evidence_toolset.materialize_source_passages(
                    source_ref=source_ref,
                    passage_refs=finding_passage_refs,
                    allowed_read_kinds={"sequential"},
                )
                if not passage_result.get("accepted"):
                    return {**passage_result, "finding_index": index}
                finding_evidence_ids = public_api._dedupe(
                    [
                        *finding_evidence_ids,
                        *[
                            str(item)
                            for item in passage_result.get("evidence_ids") or []
                            if str(item).strip()
                        ],
                    ]
                )
            if not finding_evidence_ids:
                return {
                    "accepted": False,
                    "error": "Each finding requires at least one evidence_id or passage_ref",
                    "finding_index": index,
                }
            evidence_error = await self._validate_set_evidence_ids(
                source_set=source_set,
                evidence_ids=finding_evidence_ids,
                required_source_id=source_id,
                selection_purpose="source_mapping",
                selection_role="triage_source_mapping",
            )
            if evidence_error:
                return {**evidence_error, "finding_index": index}
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(finding_evidence_ids)
            if blocked_evidence_ids:
                return {
                    "accepted": False,
                    "error": "Shell/error/access-page evidence cannot support a source mapping finding",
                    "finding_index": index,
                    "invalid_evidence_ids": blocked_evidence_ids,
                }
            question_ids = public_api._dedupe(
                [str(item) for item in raw_finding.get("question_ids") or [] if str(item).strip()]
            )
            invalid_question_ids = sorted(set(question_ids) - known_leaf_question_ids)
            if invalid_question_ids:
                return {
                    "accepted": False,
                    "error": "finding question_ids must be valid leaf question IDs",
                    "finding_index": index,
                    "invalid_question_ids": invalid_question_ids,
                    "allowed_question_ids": sorted(known_leaf_question_ids),
                }
            limitations = [
                str(item).strip() for item in raw_finding.get("limitations") or [] if str(item).strip()
            ]
            limitations_error = semantic_list_error(
                field="finding_limitations",
                values=limitations,
                max_items=TRIAGE_LIMITATION_LIMIT,
                item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
            )
            if limitations_error:
                return limitations_error
            finding_id = str(raw_finding.get("finding_id") or "").strip() or f"finding:{source_ref}:{index + 1}"
            findings.append(
                {
                    "finding_id": finding_id,
                    "source_ref": source_ref,
                    "source_id": source_id,
                    "source_version_ids": _source_version_ids(self.inventory, source_id),
                    "assessment_id": f"analysis:{source_ref}",
                    "statement": statement,
                    "support_explanation": "",
                    "evidence_ids": finding_evidence_ids,
                    "passage_refs": finding_passage_refs,
                    "scope": {"actor": "", "period": "", "geography": "", "qualifiers": []},
                    "limitations": limitations,
                    "possible_question_ids": question_ids,
                    "question_links": [],
                    "source_relations": [],
                    "semantic_revision": int(current.get("triage_semantic_revision") or 0) + 1,
                    "set_id": set_id,
                    "created_at": public_api._now(),
                    "updated_at": public_api._now(),
                }
            )
            evidence_ids.extend(finding_evidence_ids)

        raw_gaps = args.get("gaps") or []
        if not isinstance(raw_gaps, list):
            return {"accepted": False, "error": "gaps must be an array"}
        gaps: list[dict[str, str]] = []
        for index, raw_gap in enumerate(raw_gaps):
            if not isinstance(raw_gap, dict):
                return {"accepted": False, "error": "Each gap must be an object", "gap_index": index}
            question_id = str(raw_gap.get("question_id") or "").strip()
            if question_id not in known_leaf_question_ids:
                return {
                    "accepted": False,
                    "error": "gap question_id must be a valid leaf question ID",
                    "gap_index": index,
                    "question_id": question_id,
                    "allowed_question_ids": sorted(known_leaf_question_ids),
                }
            description = str(raw_gap.get("description") or "").strip()
            if not description:
                return {"accepted": False, "error": "Each gap requires description", "gap_index": index}
            field_error = semantic_field_error(
                {"gap_description": description},
                {"gap_description": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"]},
                ["gap_description"],
            )
            if field_error:
                return field_error
            gaps.append({"question_id": question_id, "description": description})

        evidence_ids = public_api._dedupe(evidence_ids)
        if status == "usable" and not evidence_ids and not duplicate_allowed and raw_findings:
            return {
                "accepted": False,
                "error": "usable source analysis requires evidence-backed findings unless it is a backend exact duplicate",
                "source_ref": source_ref,
            }
        if status == "usable" and not findings and not duplicate_allowed and not status_reason:
            return {
                "accepted": False,
                "error": "usable source analysis with no findings requires status_reason",
                "source_ref": source_ref,
                "next_action": "If the readable source has no salient material, save usable with empty findings and a concrete status_reason.",
            }
        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose="source_mapping",
            role="triage_source_mapping",
            rationale="Persisted by source mapper save_source_analysis.",
        )
        if selection_error:
            return selection_error

        previous_current = [
            item
            for item in current.get("source_analyses") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref
        ]
        history = [dict(item, current=False) for item in previous_current]
        if history:
            current.setdefault("source_analysis_history", []).extend(history)
        current["source_analyses"] = [
            item
            for item in current.get("source_analyses") or []
            if not (isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref)
        ]
        now = public_api._now()
        analysis = {
            "analysis_id": f"analysis:{source_ref}",
            "source_ref": source_ref,
            "source_id": source_id,
            "source_version_ids": _source_version_ids(self.inventory, source_id),
            "set_id": set_id,
            "status": status,
            "status_reason": status_reason,
            "summary": summary,
            "source_limitations": source_limitations,
            "read_state": {
                "complete": read_complete,
                "unread_regions": unread_regions,
                "stopping_reason": stopping_reason,
                "backend_complete": backend_read_complete,
                "backend_eof": has_delivered_source_eof(current, set_id=set_id, source_ref=source_ref),
                "backend_representation_failure": bool(representation_failure),
            },
            "findings": [
                {
                    "finding_id": item["finding_id"],
                    "statement": item["statement"],
                    "evidence_ids": item["evidence_ids"],
                    "question_ids": item["possible_question_ids"],
                    "limitations": item["limitations"],
                }
                for item in findings
            ],
            "gaps": gaps,
            "duplicate_of_source_ref": duplicate_of_source_ref,
            "current": True,
            "accepted": True,
            "attempt": int(current.get("attempt") or 1),
            "created_at": now,
            "updated_at": now,
        }
        current["source_analyses"].append(analysis)

        current["source_findings"] = [
            item
            for item in current.get("source_findings") or []
            if not (isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref)
        ]
        current["source_findings"].extend(findings)
        assessment = _legacy_assessment_from_source_analysis(
            analysis,
            evidence_ids=evidence_ids,
            current=current,
        )
        current["source_assessments"] = [
            item
            for item in current.get("source_assessments") or []
            if not (isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref)
        ]
        current["source_assessments"].append(assessment)
        _sync_source_analysis_deferred_index(current, analysis)
        current["triage_semantic_revision"] = int(current.get("triage_semantic_revision") or 0) + 1
        clear_pending_exact_read(source_set, source_ref=source_ref)
        clear_pending_comparison_read(source_set, source_ref=source_ref)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "analysis_id": analysis["analysis_id"],
            "source_ref": source_ref,
            "status": status,
            "finding_ids": [item["finding_id"] for item in findings],
            "gap_count": len(gaps),
            "next_action": "finish_source_mapping for this source, or read/save the next source.",
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

    async def _validate_source_findings(
        self,
        *,
        current: dict[str, Any],
        source_set: dict[str, Any],
        source_ref: str,
        source_id: str,
        assessment_id: str,
        raw_findings: Any,
        require_support_explanation: bool,
    ) -> tuple[list[dict[str, Any]], list[str], list[str], dict[str, Any] | None]:
        if not isinstance(raw_findings, list):
            return (
                [],
                [],
                [],
                {
                    "accepted": False,
                    "error": "findings must be an array",
                    "next_action": "Save every substantive source finding, or an empty array with no_findings_reason.",
                },
            )
        questions_by_id = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        }
        ref_context = build_source_ref_context(self.inventory)
        inventory_card = next(
            (card for card in self.inventory.source_cards if str(card.source_id) == source_id),
            None,
        )
        findings: list[dict[str, Any]] = []
        all_question_ids: list[str] = []
        all_evidence_ids: list[str] = []
        seen_finding_ids: set[str] = set()
        allowed_relationships = {
            "primary_to_secondary",
            "duplicate",
            "near_duplicate",
            "contradicts",
            "updates",
            "background_for",
        }
        allowed_source_refs = {str(item) for item in source_set.get("source_refs") or []}

        for finding_index, raw_finding in enumerate(raw_findings):
            if not isinstance(raw_finding, dict):
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Each finding must be an object",
                        "finding_index": finding_index,
                    },
                )
            statement = str(raw_finding.get("statement") or "").strip()
            if not statement:
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Each finding requires a substantive statement",
                        "finding_index": finding_index,
                    },
                )
            field_error = semantic_field_error(
                {"finding_statement": statement},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_statement"],
            )
            if field_error:
                return [], [], [], field_error
            support_explanation = str(raw_finding.get("support_explanation") or "").strip()
            support_error = semantic_field_error(
                {"finding_support_explanation": support_explanation},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_support_explanation"],
            )
            if support_error:
                return [], [], [], support_error
            if require_support_explanation and not support_explanation:
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Each finding requires an explanation of how its exact evidence supports it",
                        "finding_index": finding_index,
                    },
                )
            finding_evidence_ids = workspace_api._dedupe(
                [str(item) for item in raw_finding.get("evidence_ids") or [] if str(item).strip()]
            )
            if not finding_evidence_ids:
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Each finding requires exact saved evidence",
                        "finding_index": finding_index,
                        "next_action": "Read an exact source window and retry with its evidence_id.",
                    },
                )
            evidence_error = await self._validate_set_evidence_ids(
                source_set=source_set,
                evidence_ids=finding_evidence_ids,
                required_source_id=source_id,
                selection_purpose="coverage_outcome",
                selection_role="triage_coverage_support",
            )
            if evidence_error:
                return [], [], [], {**evidence_error, "finding_index": finding_index}
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(finding_evidence_ids)
            if blocked_evidence_ids:
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "Shell/error/access-page evidence cannot support a substantive finding",
                        "finding_index": finding_index,
                        "invalid_evidence_ids": blocked_evidence_ids,
                    },
                )

            raw_scope = raw_finding.get("scope") or {}
            if not isinstance(raw_scope, dict):
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "finding scope must be an object",
                        "finding_index": finding_index,
                    },
                )
            scope = {
                "actor": str(raw_scope.get("actor") or "").strip(),
                "period": str(raw_scope.get("period") or "").strip(),
                "geography": str(raw_scope.get("geography") or "").strip(),
                "qualifiers": [str(item).strip() for item in raw_scope.get("qualifiers") or [] if str(item).strip()],
            }
            scope_error = semantic_field_error(
                {
                    "actor": scope["actor"],
                    "period": scope["period"],
                    "geography": scope["geography"],
                },
                {key: TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"] for key in ("actor", "period", "geography")},
                ["actor", "period", "geography"],
            )
            if scope_error:
                return [], [], [], scope_error
            qualifiers_error = semantic_list_error(
                field="qualifiers",
                values=scope["qualifiers"],
                max_items=24,
                item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
            )
            if qualifiers_error:
                return [], [], [], qualifiers_error
            limitations = [str(item).strip() for item in raw_finding.get("limitations") or [] if str(item).strip()]
            limitations_error = semantic_list_error(
                field="finding_limitations",
                values=limitations,
                max_items=TRIAGE_LIMITATION_LIMIT,
                item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
            )
            if limitations_error:
                return [], [], [], limitations_error

            question_links: list[dict[str, Any]] = []
            seen_link_ids: set[str] = set()
            for raw_link in raw_finding.get("question_links") or []:
                if not isinstance(raw_link, dict):
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "finding question_links items must be objects",
                            "finding_index": finding_index,
                        },
                    )
                question_id = str(raw_link.get("question_id") or "").strip()
                question = questions_by_id.get(question_id)
                if question is None:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "finding links must reference persisted questions",
                            "finding_index": finding_index,
                            "unknown_question_ids": [question_id],
                        },
                    )
                if question_id in seen_link_ids:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "A finding may contain only one link record per question",
                            "finding_index": finding_index,
                            "question_id": question_id,
                        },
                    )
                seen_link_ids.add(question_id)
                known_requirement_ids = {
                    str(item.get("requirement_id") or "")
                    for item in question.get("answer_requirements") or []
                    if isinstance(item, dict)
                }
                requirement_ids = workspace_api._dedupe(
                    [str(item) for item in raw_link.get("requirement_ids") or [] if str(item).strip()]
                )
                unknown_requirement_ids = sorted(set(requirement_ids) - known_requirement_ids)
                if unknown_requirement_ids:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "finding requirement links must belong to the linked question",
                            "finding_index": finding_index,
                            "question_id": question_id,
                            "unknown_requirement_ids": unknown_requirement_ids,
                        },
                    )
                if known_requirement_ids and not requirement_ids:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "Link the finding to the specific answer requirements it supports",
                            "finding_index": finding_index,
                            "question_id": question_id,
                            "allowed_requirement_ids": sorted(known_requirement_ids),
                        },
                    )
                rationale = str(raw_link.get("rationale") or "").strip()
                rationale_error = semantic_field_error(
                    {"finding_link_rationale": rationale},
                    TRIAGE_SEMANTIC_FIELD_LIMITS,
                    ["finding_link_rationale"],
                )
                if rationale_error:
                    return [], [], [], rationale_error
                question_links.append(
                    {
                        "question_id": question_id,
                        "requirement_ids": requirement_ids,
                        "rationale": rationale,
                    }
                )
                all_question_ids.append(question_id)

            possible_question_ids = workspace_api._dedupe(
                [
                    str(value)
                    for value in raw_finding.get("possible_question_ids") or []
                    if str(value).strip()
                ]
            )
            possible_question_error = possible_question_ids_error(
                current,
                possible_question_ids,
                finding_index=finding_index,
            )
            if possible_question_error:
                return (
                    [],
                    [],
                    [],
                    {**possible_question_error, "source_ref": source_ref},
                )
            all_question_ids.extend(possible_question_ids)

            source_relations: list[dict[str, Any]] = []
            for raw_relation in raw_finding.get("source_relations") or []:
                if not isinstance(raw_relation, dict):
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "finding source_relations items must be objects",
                            "finding_index": finding_index,
                        },
                    )
                related_ref = str(raw_relation.get("related_source_ref") or "").strip()
                if related_ref == source_ref or related_ref not in allowed_source_refs:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "A source relation must name another source in the current set",
                            "finding_index": finding_index,
                            "related_source_ref": related_ref,
                        },
                    )
                related_source_id = resolve_source_ref(related_ref, ref_context)
                if not related_source_id:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "related_source_ref is not in the current corpus view",
                            "finding_index": finding_index,
                            "related_source_ref": related_ref,
                        },
                    )
                relationship = str(raw_relation.get("relationship") or "").strip()
                if relationship not in allowed_relationships:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "invalid source finding relationship",
                            "finding_index": finding_index,
                            "relationship": relationship,
                        },
                    )
                relation_rationale = str(raw_relation.get("rationale") or "").strip()
                relation_error = semantic_field_error(
                    {"finding_relation_rationale": relation_rationale},
                    TRIAGE_SEMANTIC_FIELD_LIMITS,
                    ["finding_relation_rationale"],
                )
                if relation_error:
                    return [], [], [], relation_error
                relation_evidence_ids = workspace_api._dedupe(
                    [str(item) for item in raw_relation.get("evidence_ids") or [] if str(item).strip()]
                )
                if not relation_evidence_ids:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "An established source relation requires exact saved evidence",
                            "finding_index": finding_index,
                            "related_source_ref": related_ref,
                        },
                    )
                relation_evidence_error = await self._validate_set_evidence_ids(
                    source_set=source_set,
                    evidence_ids=relation_evidence_ids,
                    required_source_ids={source_id, related_source_id},
                    selection_purpose="source_relationship",
                    selection_role="triage_source_relationship",
                )
                if relation_evidence_error:
                    return [], [], [], {**relation_evidence_error, "finding_index": finding_index}
                blocked_relation_evidence_ids = await self._blocked_shell_evidence_ids(relation_evidence_ids)
                if blocked_relation_evidence_ids:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "Shell/error/access-page evidence cannot establish a source relation",
                            "finding_index": finding_index,
                            "invalid_evidence_ids": blocked_relation_evidence_ids,
                        },
                    )
                relation_evidence_source_ids: set[str] = set()
                for evidence_id in relation_evidence_ids:
                    evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                    if evidence is not None:
                        relation_evidence_source_ids.add(str(evidence.source_id))
                missing_evidence_source_refs = [
                    ref
                    for ref, expected_source_id in (
                        (source_ref, source_id),
                        (related_ref, related_source_id),
                    )
                    if expected_source_id not in relation_evidence_source_ids
                ]
                if missing_evidence_source_refs:
                    return (
                        [],
                        [],
                        [],
                        {
                            "accepted": False,
                            "error": "A source relation requires grounded non-shell evidence from both sources",
                            "finding_index": finding_index,
                            "related_source_ref": related_ref,
                            "missing_evidence_source_refs": missing_evidence_source_refs,
                            "next_action": (
                                "Read exact material from each listed source_ref, then retry save_source_analysis "
                                "with both evidence_ids; otherwise remove the relation."
                            ),
                        },
                    )
                source_relations.append(
                    {
                        "related_source_ref": related_ref,
                        "related_source_id": related_source_id,
                        "relationship": relationship,
                        "rationale": relation_rationale,
                        "evidence_ids": relation_evidence_ids,
                    }
                )

            requested_finding_id = str(raw_finding.get("finding_id") or "").strip()
            finding_prefix = f"finding:{source_ref}:"
            if requested_finding_id.startswith("finding:") and not requested_finding_id.startswith(finding_prefix):
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "finding_id belongs to another source",
                        "finding_id": requested_finding_id,
                        "source_ref": source_ref,
                        "next_action": "Copy this source's exact finding_id or omit it to create a stable one.",
                    },
                )
            if requested_finding_id == finding_prefix:
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "finding_id suffix is empty",
                        "next_action": "Omit finding_id to create a stable one.",
                    },
                )
            if requested_finding_id.startswith(finding_prefix):
                finding_id = requested_finding_id
            else:
                suffix = (
                    workspace_api._stable_set_id(requested_finding_id, statement, finding_index + 1)
                    if requested_finding_id
                    else hashlib.sha256(
                        json.dumps(
                            [statement, finding_evidence_ids],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()[:12]
                )
                finding_id = f"{finding_prefix}{suffix}"
            if finding_id in seen_finding_ids:
                return (
                    [],
                    [],
                    [],
                    {
                        "accepted": False,
                        "error": "finding ids must be unique for this source analysis",
                        "finding_id": finding_id,
                    },
                )
            seen_finding_ids.add(finding_id)
            findings.append(
                {
                    "finding_id": finding_id,
                    "set_id": str(source_set.get("set_id") or ""),
                    "source_ref": source_ref,
                    "source_id": source_id,
                    "source_version_ids": list(getattr(inventory_card, "source_version_ids", []) or []),
                    "assessment_id": assessment_id,
                    "statement": statement,
                    "support_explanation": support_explanation,
                    "evidence_ids": finding_evidence_ids,
                    "passage_refs": [
                        str(value)
                        for value in raw_finding.get("passage_refs") or []
                        if str(value).strip()
                    ],
                    "scope": scope,
                    "limitations": limitations,
                    "question_links": question_links,
                    "possible_question_ids": possible_question_ids,
                    "source_relations": source_relations,
                    "updated_at": workspace_api._now(),
                }
            )
            all_evidence_ids.extend(finding_evidence_ids)

        return (
            findings,
            workspace_api._dedupe(all_question_ids),
            workspace_api._dedupe(all_evidence_ids),
            None,
        )


def _derived_relevance(
    *,
    representation_status: str,
    findings: list[dict[str, Any]],
    question_ids: list[str],
) -> str:
    if representation_status in {"not_target_content", "unreadable"}:
        return "unknown"
    if question_ids:
        return "supporting"
    if findings:
        return "contextual"
    return "out_of_scope"


def _leaf_question_ids(workspace: dict[str, Any]) -> set[str]:
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    parent_ids = {
        str(item.get("parent_question_id") or "")
        for item in questions
        if str(item.get("parent_question_id") or "").strip()
    }
    question_ids = {
        str(item.get("question_id") or "")
        for item in questions
        if str(item.get("question_id") or "").strip()
    }
    leaves = question_ids - parent_ids
    return leaves or question_ids


def _source_version_ids(inventory: Any, source_id: str) -> list[str]:
    card = next((item for item in inventory.source_cards if str(item.source_id) == source_id), None)
    return list(getattr(card, "source_version_ids", []) or [])


def _legacy_assessment_from_source_analysis(
    analysis: dict[str, Any],
    *,
    evidence_ids: list[str],
    current: dict[str, Any],
) -> dict[str, Any]:
    status = str(analysis.get("status") or "")
    source_ref = str(analysis.get("source_ref") or "")
    source_id = str(analysis.get("source_id") or "")
    question_ids = workspace_api._dedupe(
        [
            str(question_id)
            for finding in analysis.get("findings") or []
            if isinstance(finding, dict)
            for question_id in finding.get("question_ids") or []
            if str(question_id).strip()
        ]
    )
    usable = status == "usable"
    deferred = status in {"deferred", "not_target_content"}
    return {
        "assessment_id": f"assessment:{source_id}",
        "analysis_id": analysis.get("analysis_id"),
        "set_id": analysis.get("set_id") or "",
        "source_ref": source_ref,
        "source_id": source_id,
        "source_version_ids": list(analysis.get("source_version_ids") or []),
        "processing_status": "resolved",
        "representation_status": "readable" if usable else ("not_target_content" if status == "not_target_content" else "partial"),
        "representation_reason": str(analysis.get("status_reason") or ""),
        "relevance": "supporting" if question_ids else ("contextual" if usable else "unknown"),
        "relevance_reason": "",
        "authority": "unknown",
        "authority_reason": "",
        "uniqueness": "duplicate" if analysis.get("duplicate_of_source_ref") else "unknown",
        "uniqueness_reason": "",
        "duplicate_of_source_ref": analysis.get("duplicate_of_source_ref") or "",
        "disposition": "supporting" if question_ids else ("background" if usable else "deferred"),
        "disposition_reason": str(analysis.get("status_reason") or "") if deferred else "",
        "coverage_question_ids": question_ids,
        "editorial_functions": [],
        "evidence_ids": workspace_api._dedupe(evidence_ids),
        "finding_ids": [
            str(item.get("finding_id") or "")
            for item in analysis.get("findings") or []
            if isinstance(item, dict) and str(item.get("finding_id") or "")
        ],
        "reading_basis": {
            "coverage_summary": analysis.get("summary") or "",
            "unread_regions": "; ".join((analysis.get("read_state") or {}).get("unread_regions") or []),
            "stopping_reason": (analysis.get("read_state") or {}).get("stopping_reason") or "",
        },
        "question_dispositions": [],
        "source_summary": analysis.get("summary") or "",
        "no_findings_reason": "" if analysis.get("findings") else (analysis.get("status_reason") or "No findings saved."),
        "limitations": list(analysis.get("source_limitations") or []),
        "confidence": 0.5,
        "semantic_revision": int(current.get("triage_semantic_revision") or 0) + 1,
        "updated_at": workspace_api._now(),
    }


def _backend_duplicate_allowed(
    workspace: dict[str, Any],
    *,
    source_ref: str,
    duplicate_of_source_ref: str,
) -> bool:
    if not duplicate_of_source_ref or duplicate_of_source_ref == source_ref:
        return False
    metadata = workspace.get("exact_duplicate_sources") or {}
    if isinstance(metadata, dict) and str(metadata.get(source_ref) or "") == duplicate_of_source_ref:
        return True
    for item in workspace.get("source_capsules") or {}:
        capsule = (workspace.get("source_capsules") or {}).get(item)
        if (
            str(item) == source_ref
            and isinstance(capsule, dict)
            and str(capsule.get("duplicate_of_source_ref") or "") == duplicate_of_source_ref
        ):
            return True
    return False


def _sync_source_analysis_deferred_index(
    workspace: dict[str, Any],
    analysis: dict[str, Any],
) -> None:
    source_ref = str(analysis.get("source_ref") or "")
    if not source_ref:
        return
    status = str(analysis.get("status") or "")
    deferred = status in {"deferred", "not_target_content"}
    existing = [
        item
        for item in workspace.get("deferred_sources") or []
        if not (isinstance(item, dict) and str(item.get("source_ref") or "") == source_ref)
    ]
    if deferred:
        existing.append(
            {
                "source_ref": source_ref,
                "reason": str(analysis.get("status_reason") or "Source mapping deferred.").strip(),
                "status": status,
                "analysis_id": analysis.get("analysis_id") or "",
            }
        )
    workspace["deferred_sources"] = existing


__all__ = ["SourceTriageWorkspaceSourceAnalysisHandlersMixin"]
