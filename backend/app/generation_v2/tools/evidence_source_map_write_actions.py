from __future__ import annotations

from app.generation_v2.contracts import V2ToolName
from typing import Any

from app.generation_v2.source_triage_workspace import mark_workspace_write_action
from app.generation_v2.tools.evidence_source_map import _relationship_kind
from app.generation_v2.tools.evidence_utils import _exact_quote_text_matches
from app.generation_v2.tools.evidence_write_validation import validate_source_map_write_args
from app.models.generation_v2 import EvidenceStrength


async def upsert_source_card(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("upsert_source_card", args)
    if validation_error:
        return validation_error
    source_ref = str(args.get("source_ref") or args.get("source_id") or "")
    scope_error = await owner._triage_scope_error_for_refs([source_ref] if source_ref else [])
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
        return {"accepted": False, "error": "upsert_source_card requires at least one evidence_id from read/search tools"}
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
    locator = str(args.get("locator") or "").strip()
    if not quote:
        return {"accepted": False, "error": "quote is required"}
    if not locator:
        return {"accepted": False, "error": "locator is required"}
    evidence = await owner._evidence_in_run(evidence_id)
    if evidence is None:
        return {"accepted": False, "error": "evidence_id must exist in this run before creating a quote card"}
    resolved_ref = await owner._source_ref_for_id(evidence.source_id)
    scope_error = await owner._triage_scope_error_for_refs([resolved_ref])
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
    if not _exact_quote_text_matches(quote, evidence.snippet_text):
        return {"accepted": False, "error": "quote text is not found in selected evidence snippet/window"}
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
    return {"accepted": True, "source_ref": resolved_ref, "evidence_id": evidence_id}


async def record_source_relationship(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("record_source_relationship", args)
    if validation_error:
        return validation_error
    raw_refs = [
        str(args.get("source_ref") or args.get("source_id") or ""),
        str(args.get("related_source_ref") or args.get("related_source_id") or ""),
    ]
    scope_error = await owner._triage_scope_error_for_refs([ref for ref in raw_refs if ref])
    if scope_error:
        return scope_error
    source_id = await owner._source_id_from_ref_or_id(str(args.get("source_ref") or args.get("source_id") or "")) or ""
    related_source_id = (
        await owner._source_id_from_ref_or_id(str(args.get("related_source_ref") or args.get("related_source_id") or ""))
        or ""
    )
    if not owner._source_in_run_view(source_id) or not owner._source_in_run_view(related_source_id):
        return {"accepted": False, "error": "source_ref and related_source_ref must be in this run corpus view"}
    relationship = _relationship_kind(str(args.get("relationship") or ""))
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
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
    if relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"} and not evidence_ids:
        return {"accepted": False, "error": f"{relationship} relationship requires evidence_ids from read/search tools"}
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.RECORD_SOURCE_RELATIONSHIP,
        source_refs=[await owner._source_ref_for_id(source_id), await owner._source_ref_for_id(related_source_id)],
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
    scope_error = await owner._triage_scope_error_for_refs([*source_refs, *primary_refs, *secondary_refs, *duplicate_refs])
    if scope_error:
        return scope_error
    source_ids, invalid_sources = await owner._source_ids_from_refs_or_ids(source_refs)
    primary_ids, invalid_primary = await owner._source_ids_from_refs_or_ids(primary_refs)
    secondary_ids, invalid_secondary = await owner._source_ids_from_refs_or_ids(secondary_refs)
    duplicate_ids, invalid_duplicate = await owner._source_ids_from_refs_or_ids(duplicate_refs)
    invalid_refs = [*invalid_sources, *invalid_primary, *invalid_secondary, *invalid_duplicate]
    if invalid_refs:
        return {"accepted": False, "error": "cluster source_refs must be in this run corpus view", "invalid_source_refs": invalid_refs}
    label = str(args.get("label") or "").strip()
    rationale = str(args.get("rationale") or "").strip()
    if not label:
        return {"accepted": False, "error": "label is required"}
    if len(source_ids) < 2:
        return {"accepted": False, "error": "record_source_cluster requires at least two source_refs"}
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
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
    scope_error = await owner._triage_scope_error_for_refs(source_refs)
    if scope_error:
        return scope_error
    source_ids, invalid_sources = await owner._source_ids_from_refs_or_ids(source_refs)
    if invalid_sources:
        return {"accepted": False, "error": "source_refs must be in this run corpus view", "invalid_source_refs": invalid_sources}
    evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
    invalid = await owner._select_evidence_ids_for_write(
        evidence_ids,
        purpose="gap",
        role=f"triage_gap:{str(args.get('kind') or 'gap')}",
        rationale="Used by record_gap.",
    )
    if invalid:
        return {"accepted": False, "error": "gap evidence_ids must be observed evidence from this run", "invalid_evidence_ids": invalid}
    if not str(args.get("description") or "").strip():
        return {"accepted": False, "error": "description is required"}
    resolved_refs = [await owner._source_ref_for_id(source_id) for source_id in source_ids]
    await mark_workspace_write_action(
        owner.db,
        generation_run_id=owner.run.id,
        tool_name=V2ToolName.RECORD_GAP,
        source_refs=resolved_refs,
        evidence_ids=evidence_ids,
    )
    return {"accepted": True, "source_count": len(source_ids), "evidence_count": len(evidence_ids)}


async def finalize_source_map(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    validation_error = validate_source_map_write_args("finalize_source_map", args)
    if validation_error:
        return validation_error
    if not bool(args.get("ready")):
        return {"accepted": False, "error": "finalize_source_map ready=false; record gaps first or keep triage unfinished"}
    if not str(args.get("summary") or "").strip():
        return {"accepted": False, "error": "summary is required"}
    selected = await owner._selected_evidence_ids()
    if not selected:
        return {"accepted": False, "error": "cannot finalize source map without selected evidence"}
    return {"accepted": True, "selected_evidence_count": len(selected)}


__all__ = [
    "create_quote_card",
    "finalize_source_map",
    "record_gap",
    "record_source_cluster",
    "record_source_relationship",
    "upsert_source_card",
]
