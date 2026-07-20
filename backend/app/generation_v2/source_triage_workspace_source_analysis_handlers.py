from __future__ import annotations

import hashlib
import json
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
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
from app.generation_v2.source_triage_workspace_tracking import clear_pending_exact_read
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.ingestion.web_quality import assess_web_fetch_health


class SourceTriageWorkspaceSourceAnalysisHandlersMixin:
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
            findings, finding_question_ids, finding_evidence_ids, finding_error = await self._validate_source_findings(
                current=current,
                source_set=source_set,
                source_ref=source_ref,
                source_id=source_id,
                assessment_id=f"assessment:{source_id}",
                raw_findings=args.get("findings"),
            )
            if finding_error:
                return finding_error
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
        requires_grounded_evidence = values["representation_status"] == "not_target_content" or (
            values["processing_status"] == "resolved" and values["representation_status"] in {"readable", "partial"}
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
            "disposition_reason": reason_fields["disposition_reason"],
            "coverage_question_ids": question_ids,
            "editorial_functions": editorial_functions,
            "evidence_ids": evidence_ids,
            "finding_ids": [str(item.get("finding_id") or "") for item in findings],
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
        analysis_changed = (
            not analysis_mode
            or _semantic_json(old_assessment) != _semantic_json(assessment)
            or _semantic_json(old_findings) != _semantic_json(findings)
        )
        if analysis_mode and analysis_changed:
            # A later source-analysis write changes the finalizer's authoritative
            # review stream. Make it read the current questions/findings again.
            current.pop("source_triage_finalizer_reads", None)
            next_revision = int(current.get("triage_semantic_revision") or 0) + 1
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
        elif analysis_mode:
            # Preserve existing revision/timestamps on a semantic no-op.
            findings = old_findings
            assessment["finding_ids"] = [str(item.get("finding_id") or "") for item in old_findings]
        current["source_assessments"] = [
            item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_id") or "") != source_id
        ]
        current["source_assessments"].append(assessment)
        clear_pending_exact_read(source_set, source_ref=source_ref)
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
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "source_ref": source_ref,
            "processing_status": values["processing_status"],
            "representation_status": values["representation_status"],
            "disposition": values["disposition"],
            "finding_ids": list(assessment.get("finding_ids") or []),
            "analysis_changed": analysis_changed,
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

    async def _validate_source_findings(
        self,
        *,
        current: dict[str, Any],
        source_set: dict[str, Any],
        source_ref: str,
        source_id: str,
        assessment_id: str,
        raw_findings: Any,
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
                    "evidence_ids": finding_evidence_ids,
                    "scope": scope,
                    "limitations": limitations,
                    "question_links": question_links,
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


__all__ = ["SourceTriageWorkspaceSourceAnalysisHandlersMixin"]
