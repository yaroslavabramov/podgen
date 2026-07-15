from __future__ import annotations

from app.generation_v2.contracts import V2ToolName
from typing import Any

from app.generation_v2.source_triage_workspace import mark_workspace_write_action
from app.generation_v2.tools.evidence_source_map import _relationship_kind
from app.generation_v2.tools.evidence_source_map_action_helpers import cluster_id as _cluster_id
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref
from app.generation_v2.tools.evidence_utils import _dedupe, _exact_quote_source_span
from app.generation_v2.tools.evidence_write_validation import validate_source_map_write_args
from app.models.generation_v2 import EvidenceStrength


async def _evidence_source_scope_error(
    owner: Any,
    *,
    evidence_ids: list[str],
    allowed_source_ids: set[str],
    error: str,
) -> dict[str, Any] | None:
    """Validate the complete batch before selection can mutate evidence state."""

    invalid_evidence_ids: list[str] = []
    for evidence_id in dict.fromkeys(evidence_ids):
        evidence = await owner._evidence_in_run(evidence_id)
        if evidence is None or str(evidence.source_id) not in allowed_source_ids:
            invalid_evidence_ids.append(evidence_id)
    if not invalid_evidence_ids:
        return None
    return {
        "accepted": False,
        "error": error,
        "invalid_evidence_ids": invalid_evidence_ids,
    }


async def upsert_source_card(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("upsert_source_card", args)
    if validation_error:
        return validation_error
    source_ref = str(args.get("source_ref") or args.get("source_id") or "")
    scope_error = await owner._triage_write_scope_error_for_refs([source_ref] if source_ref else [])
    if scope_error:
        return scope_error
    source_id = await owner._source_id_from_ref_or_id(source_ref) or ""
    if not owner._source_in_run_view(source_id):
        return {"accepted": False, "error": "source_ref is not in this run corpus view"}
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
    invalid = await owner._select_evidence_ids_for_write(
        evidence_ids,
        source_id=source_id,
        purpose="source_card",
        role="triage_source_card",
        rationale="Used by upsert_source_card.",
    )
    if invalid:
        return {
            "accepted": False,
            "error": "evidence_ids must be observed evidence from source_ref in this run",
            "invalid_evidence_ids": invalid,
        }
    if not evidence_ids:
        return {
            "accepted": False,
            "error": "upsert_source_card requires at least one evidence_id from read/search tools",
        }
    resolved_ref = await owner._source_ref_for_id(source_id)
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.UPSERT_SOURCE_CARD,
        source_refs=[resolved_ref],
        evidence_ids=evidence_ids,
    )
    return {"accepted": True, "source_ref": resolved_ref, "evidence_count": len(evidence_ids)}


async def create_quote_card(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("create_quote_card", args)
    if validation_error:
        return validation_error
    source_id = str(args.get("source_id") or "")
    source_version_id = str(args.get("source_version_id") or "")
    evidence_id = str(args.get("evidence_id") or "")
    quote = str(args.get("quote") or "").strip()
    if not quote:
        return {"accepted": False, "error": "quote is required"}
    evidence = await owner._evidence_in_run(evidence_id)
    if evidence is None:
        return {"accepted": False, "error": "evidence_id must exist in this run before creating a quote card"}
    canonical_locator = str(evidence.locator_text or "").strip()
    if not canonical_locator:
        return {
            "accepted": False,
            "error": "quote evidence must have a canonical locator before creating a quote card",
        }
    resolved_ref = await owner._source_ref_for_id(evidence.source_id)
    scope_error = await owner._triage_write_scope_error_for_refs([resolved_ref])
    if scope_error:
        return scope_error
    if source_id and str(evidence.source_id) != source_id:
        return {"accepted": False, "error": "provided source does not match selected evidence"}
    if source_version_id and str(evidence.source_version_id) != source_version_id:
        return {"accepted": False, "error": "provided source version does not match selected evidence"}
    if evidence.evidence_strength != EvidenceStrength.EXACT:
        return {
            "accepted": False,
            "error": "quote evidence is not canonically verified; keep it as a paraphrase instead of a quote card",
        }
    canonical_quote = _exact_quote_source_span(quote, evidence.snippet_text)
    if canonical_quote is None:
        return {"accepted": False, "error": "quote text is not found in selected evidence snippet/window"}
    # The write-action trace is the canonical SourceMap input. Replace a
    # model-normalized rendering with the exact span read from source text.
    args["quote"] = canonical_quote
    invalid = await owner._select_evidence_ids_for_write(
        [evidence_id],
        purpose="quote_card",
        role="triage_quote_seed",
        rationale="Used by create_quote_card.",
    )
    if invalid:
        return {"accepted": False, "error": "quote evidence could not be selected", "invalid_evidence_ids": invalid}
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.CREATE_QUOTE_CARD,
        source_refs=[resolved_ref],
        evidence_ids=[evidence_id],
    )
    return {
        "accepted": True,
        "source_ref": resolved_ref,
        "evidence_id": evidence_id,
        "locator": canonical_locator,
    }


async def record_source_relationship(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("record_source_relationship", args)
    if validation_error:
        return validation_error
    raw_refs = [
        str(args.get("source_ref") or args.get("source_id") or ""),
        str(args.get("related_source_ref") or args.get("related_source_id") or ""),
    ]
    scope_error = await owner._triage_write_scope_error_for_refs([ref for ref in raw_refs if ref])
    if scope_error:
        return scope_error
    source_id = await owner._source_id_from_ref_or_id(str(args.get("source_ref") or args.get("source_id") or "")) or ""
    related_source_id = (
        await owner._source_id_from_ref_or_id(
            str(args.get("related_source_ref") or args.get("related_source_id") or "")
        )
        or ""
    )
    if not owner._source_in_run_view(source_id) or not owner._source_in_run_view(related_source_id):
        return {"accepted": False, "error": "source_ref and related_source_ref must be in this run corpus view"}
    if source_id == related_source_id:
        return {
            "accepted": False,
            "error": "source_ref and related_source_ref must identify two different sources",
        }
    raw_relationship = str(args.get("relationship") or "")
    allowed_relationships = {
        "primary_to_secondary",
        "duplicate",
        "near_duplicate",
        "contradicts",
        "updates",
        "background_for",
    }
    if raw_relationship not in allowed_relationships:
        return {
            "accepted": False,
            "error": "relationship must use a canonical enum literal",
            "relationship": raw_relationship,
        }
    relationship = _relationship_kind(raw_relationship)
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
    evidence_scope_error = await _evidence_source_scope_error(
        owner,
        evidence_ids=evidence_ids,
        allowed_source_ids={source_id, related_source_id},
        error="relationship evidence_ids must belong to source_ref or related_source_ref",
    )
    if evidence_scope_error:
        return evidence_scope_error
    invalid = await owner._select_evidence_ids_for_write(
        evidence_ids,
        purpose="source_relationship",
        role=f"triage_relationship:{relationship}",
        rationale="Used by record_source_relationship.",
    )
    if invalid:
        return {
            "accepted": False,
            "error": "relationship evidence_ids must be observed evidence from this run",
            "invalid_evidence_ids": invalid,
        }
    if (
        relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"}
        and not evidence_ids
    ):
        return {"accepted": False, "error": f"{relationship} relationship requires evidence_ids from read/search tools"}
    source_ref = await owner._source_ref_for_id(source_id)
    related_source_ref = await owner._source_ref_for_id(related_source_id)
    if relationship in {"duplicate", "near_duplicate"} and related_source_ref < source_ref:
        args["canonical_source_id"] = related_source_id
        args["canonical_related_source_id"] = source_id
    elif relationship in {"duplicate", "near_duplicate"}:
        args["canonical_source_id"] = source_id
        args["canonical_related_source_id"] = related_source_id
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.RECORD_SOURCE_RELATIONSHIP,
        source_refs=[source_ref, related_source_ref],
        evidence_ids=evidence_ids,
    )
    return {"accepted": True, "relationship": relationship, "evidence_count": len(evidence_ids)}


async def record_source_cluster(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("record_source_cluster", args)
    if validation_error:
        return validation_error
    source_refs = [str(item) for item in args.get("source_refs") or args.get("source_ids") or [] if item]
    primary_refs = [str(item) for item in args.get("primary_source_refs") or [] if item]
    secondary_refs = [str(item) for item in args.get("secondary_source_refs") or [] if item]
    duplicate_refs = [str(item) for item in args.get("duplicate_source_refs") or [] if item]
    scope_error = await owner._triage_write_scope_error_for_refs(
        [*source_refs, *primary_refs, *secondary_refs, *duplicate_refs]
    )
    if scope_error:
        return scope_error
    source_ids, invalid_sources = await owner._source_ids_from_refs_or_ids(source_refs)
    primary_ids, invalid_primary = await owner._source_ids_from_refs_or_ids(primary_refs)
    secondary_ids, invalid_secondary = await owner._source_ids_from_refs_or_ids(secondary_refs)
    duplicate_ids, invalid_duplicate = await owner._source_ids_from_refs_or_ids(duplicate_refs)
    invalid_refs = [*invalid_sources, *invalid_primary, *invalid_secondary, *invalid_duplicate]
    if invalid_refs:
        return {
            "accepted": False,
            "error": "cluster source_refs must be in this run corpus view",
            "invalid_source_refs": invalid_refs,
        }
    label = str(args.get("label") or "").strip()
    rationale = str(args.get("rationale") or "").strip()
    if not label:
        return {"accepted": False, "error": "label is required"}
    if len(source_ids) < 2:
        return {"accepted": False, "error": "record_source_cluster requires at least two source_refs"}
    cluster_source_ids = set(source_ids)
    role_ids = {
        "primary_source_refs": set(primary_ids),
        "secondary_source_refs": set(secondary_ids),
        "duplicate_source_refs": set(duplicate_ids),
    }
    roles_outside_cluster = [name for name, values in role_ids.items() if values - cluster_source_ids]
    if roles_outside_cluster:
        return {
            "accepted": False,
            "error": "every cluster role source_ref must also appear in source_refs",
            "invalid_role_fields": roles_outside_cluster,
        }
    overlapping_roles = [
        f"{left}+{right}"
        for index, (left, left_ids) in enumerate(role_ids.items())
        for right, right_ids in list(role_ids.items())[index + 1 :]
        if left_ids & right_ids
    ]
    if overlapping_roles:
        return {
            "accepted": False,
            "error": "a source cannot have incompatible roles in the same cluster",
            "overlapping_role_fields": overlapping_roles,
        }
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
    evidence_scope_error = await _evidence_source_scope_error(
        owner,
        evidence_ids=evidence_ids,
        allowed_source_ids=cluster_source_ids,
        error="cluster evidence_ids must belong to a source_ref declared in this cluster",
    )
    if evidence_scope_error:
        return evidence_scope_error
    invalid = await owner._select_evidence_ids_for_write(
        evidence_ids,
        purpose="source_cluster",
        role="triage_source_cluster",
        rationale="Used by record_source_cluster.",
    )
    if invalid:
        return {
            "accepted": False,
            "error": "cluster evidence_ids must be observed evidence from this run",
            "invalid_evidence_ids": invalid,
        }
    if (primary_ids or secondary_ids or duplicate_ids) and not evidence_ids:
        return {"accepted": False, "error": "primary/secondary/duplicate cluster claims require evidence_ids"}
    resolved_refs = [await owner._source_ref_for_id(source_id) for source_id in source_ids]
    # Persist a collision-resistant identity only in the accepted trace. The
    # model continues to use the semantic label and public source refs.
    args["cluster_id"] = _cluster_id(label, 1, source_ids=source_ids)
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.RECORD_SOURCE_CLUSTER,
        source_refs=resolved_refs,
        evidence_ids=evidence_ids,
    )
    return {
        "accepted": True,
        "source_count": len(source_ids),
        "primary_source_count": len(primary_ids),
        "secondary_source_count": len(secondary_ids),
        "duplicate_source_count": len(duplicate_ids),
        "evidence_count": len(evidence_ids),
        "has_rationale": bool(rationale),
    }


async def record_gap(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("record_gap", args)
    if validation_error:
        return validation_error
    source_refs = [str(item) for item in args.get("source_refs") or args.get("source_ids") or [] if item]
    scope_error = await owner._triage_write_scope_error_for_refs(source_refs)
    if scope_error:
        return scope_error
    source_ids, invalid_sources = await owner._source_ids_from_refs_or_ids(source_refs)
    if invalid_sources:
        return {
            "accepted": False,
            "error": "source_refs must be in this run corpus view",
            "invalid_source_refs": invalid_sources,
        }
    if not str(args.get("description") or "").strip():
        return {"accepted": False, "error": "description is required"}
    question_ids = _dedupe(
        [str(question_id) for question_id in args.get("question_ids") or [] if str(question_id).strip()]
    )
    if question_ids:
        from app.generation_v2.source_triage_workspace_persistence import load_source_triage_workspace

        workspace = await load_source_triage_workspace(owner.db, owner.run.id)
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in (workspace or {}).get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "").strip()
        }
        invalid_question_ids = sorted(set(question_ids) - known_question_ids)
        if invalid_question_ids:
            return {
                "accepted": False,
                "error": "gap question_ids must identify persisted questions in the current run",
                "invalid_question_ids": invalid_question_ids,
            }
        current_set_id = str(getattr(owner, "triage_current_set_id", None) or "")
        if current_set_id:
            current_set = next(
                (
                    item
                    for item in (workspace or {}).get("source_sets") or []
                    if isinstance(item, dict) and str(item.get("set_id") or "") == current_set_id
                ),
                {},
            )
            invalid_question_ids = sorted(
                set(question_ids) - {str(item) for item in current_set.get("question_ids") or []}
            )
            if invalid_question_ids:
                return {
                    "accepted": False,
                    "error": "gap question_ids must belong to the current source set",
                    "invalid_question_ids": invalid_question_ids,
                }
    args["question_ids"] = question_ids
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
    evidence_scope_error = await _evidence_source_scope_error(
        owner,
        evidence_ids=evidence_ids,
        allowed_source_ids=set(source_ids),
        error="gap evidence_ids must belong to a source_ref declared for this gap",
    )
    if evidence_scope_error:
        return evidence_scope_error
    invalid = await owner._select_evidence_ids_for_write(
        evidence_ids,
        purpose="gap",
        role=f"triage_gap:{str(args.get('kind') or 'gap')}",
        rationale="Used by record_gap.",
    )
    if invalid:
        return {
            "accepted": False,
            "error": "gap evidence_ids must be observed evidence from this run",
            "invalid_evidence_ids": invalid,
        }
    resolved_refs = [await owner._source_ref_for_id(source_id) for source_id in source_ids]
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.RECORD_GAP,
        source_refs=resolved_refs,
        evidence_ids=evidence_ids,
    )
    return {
        "accepted": True,
        "source_count": len(source_ids),
        "question_count": len(question_ids),
        "evidence_count": len(evidence_ids),
    }


async def list_source_map_gaps(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    source_map, inventory = await _current_source_map(owner)
    from app.generation_v2.research_source_map_prompting import source_map_research_page

    return source_map_research_page(
        source_map,
        inventory,
        section="source_map_gap_records",
        cursor=_non_negative_int(args.get("cursor")),
        limit=max(1, min(_positive_int(args.get("limit"), default=10), 40)),
        include_evidence_ids=False,
    )


async def resolve_gap(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("resolve_gap", args)
    if validation_error:
        return validation_error
    gap_ref = str(args.get("gap_ref") or "").strip().upper()
    basis_ref = str(args.get("basis_ref") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not gap_ref:
        return {"accepted": False, "error": "gap_ref is required"}
    if not reason:
        return {"accepted": False, "error": "reason is required"}

    from app.generation_v2.source_triage_workspace_persistence import load_source_triage_workspace

    workspace = await load_source_triage_workspace(owner.db, owner.run.id)
    basis = _gap_resolution_basis(workspace or {}, basis_ref)
    basis_evidence_ids = list(basis.get("evidence_ids") or [])
    if not basis_evidence_ids:
        return {
            "accepted": False,
            "error": "basis_ref must identify a current durable assessment/outcome/synthesis with saved evidence",
            "basis_ref": basis_ref,
        }
    basis_evidence: list[Any] = []
    invalid_basis_evidence: list[str] = []
    for evidence_id in basis_evidence_ids:
        evidence = await owner._evidence_in_run(evidence_id)
        if evidence is None:
            invalid_basis_evidence.append(evidence_id)
        else:
            basis_evidence.append(evidence)
    if invalid_basis_evidence:
        return {
            "accepted": False,
            "error": "basis_ref contains evidence outside the current run",
            "basis_ref": basis_ref,
            "invalid_evidence_ids": invalid_basis_evidence,
        }

    source_map, _inventory = await _current_source_map(owner)
    gap_by_ref = {public_gap_ref(record.gap_id): record for record in source_map.gap_records}
    record = gap_by_ref.get(gap_ref)
    if record is None:
        return {
            "accepted": False,
            "error": "Unknown or already resolved gap_ref; list current gaps and retry",
            "gap_ref": gap_ref,
        }
    source_refs = [await owner._source_ref_for_id(source_id) for source_id in record.source_ids]
    if not source_refs and getattr(owner, "triage_current_set_id", None):
        return {
            "accepted": False,
            "error": "A global gap can be resolved only by the run-wide triage finalizer",
            "gap_ref": gap_ref,
        }
    scope_error = await owner._triage_write_scope_error_for_refs(source_refs)
    if scope_error:
        return scope_error
    if record.question_ids:
        lineage_matches = bool(
            set(record.question_ids).intersection(basis.get("question_ids") or [])
        )
        required_lineage = "question_id"
    elif record.source_ids:
        basis_source_ids = {
            str(evidence.source_id)
            for evidence in basis_evidence
            if getattr(evidence, "source_id", None)
        }
        lineage_matches = bool(set(record.source_ids).intersection(basis_source_ids))
        required_lineage = "source_ref"
    else:
        lineage_matches = not bool(getattr(owner, "triage_current_set_id", None))
        required_lineage = "run-wide finalizer"
    if not lineage_matches:
        return {
            "accepted": False,
            "error": f"basis_ref is not linked to this gap by required {required_lineage} lineage",
            "gap_ref": gap_ref,
            "basis_ref": basis_ref,
        }

    # Persist the canonical id only in the internal trace. The model uses the
    # compact stable ref and never needs the hash-like storage identity.
    args["gap_id"] = record.gap_id
    args["source_refs"] = source_refs
    args["basis_evidence_ids"] = basis_evidence_ids
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.RESOLVE_GAP,
        source_refs=source_refs,
        evidence_ids=basis_evidence_ids,
    )
    return {"accepted": True, "gap_ref": gap_ref, "basis_ref": basis_ref, "resolved": True}


async def resolve_source_map_repair(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("resolve_source_map_repair", args)
    if validation_error:
        return validation_error
    repair_ref = str(args.get("repair_ref") or "").strip().upper()
    reason = str(args.get("reason") or "").strip()
    if not repair_ref:
        return {"accepted": False, "error": "repair_ref is required"}
    if not reason:
        return {"accepted": False, "error": "reason is required"}

    from app.generation_v2.source_triage_workspace_persistence import (
        load_source_triage_workspace,
        save_source_triage_workspace,
    )
    from app.generation_v2.source_triage_workspace_state import invalidate_coverage_syntheses, now

    workspace = await load_source_triage_workspace(owner.db, owner.run.id)
    if not workspace:
        return {"accepted": False, "error": "source_triage_workspace_not_found"}
    repair_items = [
        item for item in workspace.get("source_map_repair_items") or [] if isinstance(item, dict)
    ]
    repair_item = next(
        (item for item in repair_items if str(item.get("repair_ref") or "").upper() == repair_ref),
        None,
    )
    target = (workspace.get("source_map_repair_targets") or {}).get(repair_ref)
    if repair_item is None or not isinstance(target, dict):
        return {"accepted": False, "error": "unknown_source_map_repair_ref", "repair_ref": repair_ref}
    if str(repair_item.get("status") or "") != "open":
        return {"accepted": False, "error": "source_map_repair_already_resolved", "repair_ref": repair_ref}

    # Internal target identities remain in the durable tool trace only. The
    # model sees and submits the stable public repair_ref.
    args.update(target)
    repair_item["status"] = "resolved"
    repair_item["resolution_reason"] = reason
    repair_item["resolved_at"] = now()
    workspace["source_map_repair_items"] = repair_items
    invalidate_coverage_syntheses(workspace)
    await save_source_triage_workspace(
        owner.db,
        generation_run_id=owner.run.id,
        workspace=workspace,
    )
    return {"accepted": True, "repair_ref": repair_ref, "resolved": True}


async def _current_source_map(owner: Any):
    from app.generation_v2.tools.evidence_source_map import build_source_inventory
    from app.generation_v2.tools.evidence_source_map_actions import build_source_map_from_write_actions

    inventory = await build_source_inventory(owner.db, owner.run_view)
    source_map = await build_source_map_from_write_actions(
        owner.db,
        generation_run_id=owner.run.id,
        inventory=inventory,
        run_view=owner.run_view,
    )
    return source_map, inventory


def _non_negative_int(value: Any) -> int:
    text = str(value or "0")
    return max(0, int(text) if text.isdigit() else 0)


def _positive_int(value: Any, *, default: int) -> int:
    text = str(value or "")
    return int(text) if text.isdigit() and int(text) > 0 else default


def _gap_resolution_basis(workspace: dict[str, Any], basis_ref: str) -> dict[str, list[str]]:
    if not basis_ref:
        return {}
    source_ref_by_id = {
        str(item.get("source_id") or ""): str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and item.get("source_id") and item.get("source_ref")
    }

    def refs(item: dict[str, Any]) -> list[str]:
        return _dedupe(
            [
                *[str(value) for value in item.get("source_refs") or []],
                *[
                    source_ref_by_id.get(str(source_id), "")
                    for source_id in item.get("source_ids") or []
                ],
            ]
        )

    def payload(item: dict[str, Any]) -> dict[str, list[str]]:
        return {
            "evidence_ids": _dedupe(
                [str(value) for value in item.get("evidence_ids") or [] if str(value)]
            ),
            "source_refs": refs(item),
            "question_ids": _dedupe(
                [
                    *[str(value) for value in item.get("coverage_question_ids") or []],
                    str(item.get("question_id") or ""),
                ]
            ),
        }

    for item in workspace.get("source_assessments") or []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        if (
            basis_ref == f"assessment:{source_ref}"
            and str(item.get("processing_status") or "resolved") == "resolved"
        ):
            return payload({**item, "source_refs": [source_ref]})
    for item in workspace.get("coverage_outcomes") or []:
        if not isinstance(item, dict):
            continue
        outcome_ref = f"outcome:{str(item.get('question_id') or '')}:{str(item.get('set_id') or '')}"
        if basis_ref == outcome_ref and str(item.get("status") or "") in {
            "covered",
            "partial",
            "missing",
            "conflicted",
            "deferred",
        }:
            return payload(item)
    from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash

    for item in workspace.get("coverage_syntheses") or []:
        if not isinstance(item, dict) or basis_ref != str(item.get("outcome_id") or ""):
            continue
        question_id = str(item.get("question_id") or "")
        if (
            int(item.get("input_revision") or 0) == int(workspace.get("triage_semantic_revision") or 0)
            and str(item.get("input_hash") or "") == coverage_synthesis_input_hash(workspace, question_id)
        ):
            return payload(item)
    return {}


async def finalize_source_map(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("finalize_source_map", args)
    if validation_error:
        return validation_error
    if not bool(args.get("ready")):
        return {
            "accepted": False,
            "error": "finalize_source_map ready=false; record gaps first or keep triage unfinished",
        }
    if not str(args.get("summary") or "").strip():
        return {"accepted": False, "error": "summary is required"}
    from app.generation_v2.source_triage_canonical import canonical_workspace_violations
    from app.generation_v2.source_triage_workspace_persistence import (
        load_source_triage_workspace,
        save_source_triage_workspace,
    )

    workspace = await load_source_triage_workspace(owner.db, owner.run.id)
    if workspace and str(workspace.get("schema_version") or "") == "source_triage_workspace.v2":
        open_repairs = [
            str(item.get("repair_ref") or "")
            for item in workspace.get("source_map_repair_items") or []
            if isinstance(item, dict) and str(item.get("status") or "open") == "open"
        ]
        if open_repairs:
            return {
                "accepted": False,
                "error": "source_map_repairs_open",
                "repair_refs": open_repairs,
            }
        violations = canonical_workspace_violations(workspace, require_syntheses=True)
        if violations:
            return {
                "accepted": False,
                "error": "source_map_workspace_not_ready",
                "violations": violations,
            }
    selected = await owner._selected_evidence_ids()
    if not selected and not workspace:
        return {"accepted": False, "error": "cannot finalize legacy source map without selected evidence"}
    if workspace:
        workspace["source_map_finalization"] = {
            "ready": True,
            "summary": str(args.get("summary") or ""),
        }
        workspace.pop("source_map_repair_items", None)
        workspace.pop("source_map_repair_targets", None)
        await save_source_triage_workspace(
            owner.db,
            generation_run_id=owner.run.id,
            workspace=workspace,
        )
    return {
        "accepted": True,
        "selected_evidence_count": len(selected),
        "workspace_validated": bool(workspace),
    }


__all__ = [
    "create_quote_card",
    "finalize_source_map",
    "record_gap",
    "list_source_map_gaps",
    "resolve_gap",
    "resolve_source_map_repair",
    "record_source_cluster",
    "record_source_relationship",
    "upsert_source_card",
]
