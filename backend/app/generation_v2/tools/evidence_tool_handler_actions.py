from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_refs import source_ref_for_id
from app.generation_v2.tools.evidence_page_refs import (
    EvidencePageRefError,
    create_evidence_page_ref,
    resolve_evidence_page_ref,
)
from app.generation_v2.tools.evidence_scope import target_aware_triage_read_guard
from app.generation_v2.tools.evidence_source_map import _source_summaries
from app.generation_v2.tools.evidence_tool_payloads import (
    active_filter_list as _active_filter_list,
    compact_source_card as _compact_source_card,
)
from app.generation_v2.tools.evidence_utils import _uuid_or_none
from app.models.generation_v2 import SourceVersion


async def list_sources_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 25), 25))
    cursor = str(args.get("cursor") or "")
    offset = int(cursor) if cursor.isdigit() else 0
    context = await owner._source_ref_context()
    inventory = context.inventory
    selected = inventory.source_cards[offset : offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(inventory.source_cards) else ""
    canonical_kinds = await owner._canonical_kinds_by_source()
    return {
        "sources": [
            _compact_source_card(
                card,
                canonical_kind=canonical_kinds.get(card.source_id),
                source_ref=source_ref_for_id(card.source_id, context),
            )
            for card in selected
        ],
        "next_cursor": next_cursor,
        "total": len(inventory.source_cards),
    }


async def search_corpus_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    requested_refs = [str(item) for item in args.get("source_refs") or [] if str(item).strip()]
    guard_error = await target_aware_triage_read_guard(
        owner,
        target_source_refs=requested_refs,
        exact_read=False,
    )
    if guard_error:
        return {**guard_error, "results": []}
    query = str(args.get("query") or "").strip()
    if len(query) < 3:
        return {"accepted": False, "error": "query must contain at least 3 non-whitespace characters", "results": []}
    scope = str(args.get("scope") or "").strip() or ("selected_sources" if args.get("source_refs") else "all_sources")
    if scope not in {"all_sources", "selected_sources"}:
        return {"accepted": False, "error": "scope must be all_sources or selected_sources", "results": []}
    source_refs = [str(item) for item in args.get("source_refs") or [] if item]
    triage_allowed_refs = getattr(owner, "triage_allowed_source_refs", None)
    research_allowed_refs = getattr(owner, "research_allowed_source_refs", None)
    if triage_allowed_refs is not None and scope == "all_sources":
        return {
            "accepted": False,
            "error": "all_sources_search_not_allowed_in_current_triage_set",
            "message": "Search only within source_refs from the current source set.",
            "allowed_source_refs": sorted(triage_allowed_refs),
            "results": [],
        }
    if research_allowed_refs is not None and scope == "all_sources":
        if not bool(getattr(owner, "research_allow_broad_search", False)):
            return {
                "accepted": False,
                "error": "all_sources_search_not_allowed_in_current_research_job",
                "message": "Search only within source_refs from the current investigation job.",
                "allowed_source_refs": sorted(research_allowed_refs),
                "results": [],
            }
        if not research_allowed_refs:
            return {
                "accepted": False,
                "error": "no_eligible_sources_in_current_research_workspace",
                "message": "Triage did not leave any source eligible for broad research search.",
                "results": [],
            }
        source_refs = sorted(research_allowed_refs)
        scope = "selected_sources"
    scope_error = await owner._triage_scope_error_for_refs(source_refs)
    if scope_error:
        return {**scope_error, "results": []}
    source_ids: list[str] = []
    if scope == "selected_sources":
        if not source_refs:
            return {"accepted": False, "error": "source_refs is required when scope=selected_sources", "results": []}
        source_ids, invalid = await owner._source_ids_from_refs_or_ids(source_refs)
        if invalid:
            return {
                "accepted": False,
                "error": "source_refs include values outside this run corpus view",
                "invalid_source_refs": invalid,
                "results": [],
            }
    limit = max(1, min(int(args.get("limit") or 5), 8))
    return await owner.search_corpus(
        query=query,
        purpose=str(args.get("purpose") or "general"),
        source_ids=source_ids,
        limit=limit,
        scope=scope,
    )


async def read_evidence_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    try:
        evidence_id = uuid.UUID(str(args.get("evidence_id") or ""))
    except ValueError:
        return {"found": False, "error": "Invalid evidence_id"}
    item = await owner._evidence_in_run(str(evidence_id))
    if item is None:
        return {"found": False}
    scope_error = await owner._triage_scope_error_for_source_ids([str(item.source_id)])
    if scope_error:
        return scope_error
    source_ref_resolver = getattr(owner, "_source_ref_for_id", None)
    source_ref = (
        await source_ref_resolver(item.source_id)
        if callable(source_ref_resolver)
        else str(item.source_id)
    )
    guard_error = await target_aware_triage_read_guard(
        owner,
        target_source_refs=[source_ref],
        exact_read=True,
    )
    if guard_error:
        return guard_error
    raw_snippet = str(getattr(item, "snippet_text", "") or "")
    max_chars = max(400, min(int(args.get("max_chars") or 2400), 3000))
    requested_page_ref = str(args.get("page_ref") or "").strip()
    if requested_page_ref or len(raw_snippet) > max_chars:
        if not raw_snippet:
            return {"found": False, "error": "EvidenceItem has no readable source text."}
        if requested_page_ref:
            try:
                page = resolve_evidence_page_ref(
                    requested_page_ref,
                    evidence_id=evidence_id,
                    text=raw_snippet,
                )
            except EvidencePageRefError:
                first_end = min(len(raw_snippet), max_chars)
                return {
                    "found": False,
                    "error": "invalid_or_stale_evidence_page_ref",
                    "available_page_ref": create_evidence_page_ref(
                        evidence_id=evidence_id,
                        text=raw_snippet,
                        start_offset=0,
                        end_offset=first_end,
                    ),
                    "next_action": (
                        "Retry read_evidence with available_page_ref, or pass page_ref='' to restart from "
                        "the first exact page."
                    ),
                }
            start, end = page.start_offset, page.end_offset
        else:
            start, end = 0, min(len(raw_snippet), max_chars)
        current_page_ref = create_evidence_page_ref(
            evidence_id=evidence_id,
            text=raw_snippet,
            start_offset=start,
            end_offset=end,
        )
        payload = await owner._evidence_detail_page_payload(
            item,
            start_offset=start,
            end_offset=end,
        )
        marker = getattr(owner, "_mark_evidence_exact_read", None)
        exact_read_complete = (
            await marker(
                item,
                method="read_evidence_pages",
                read_range=(start, end),
                total_chars=len(raw_snippet),
            )
            if callable(marker)
            else False
        )
        next_page_ref = ""
        if end < len(raw_snippet):
            next_page_ref = create_evidence_page_ref(
                evidence_id=evidence_id,
                text=raw_snippet,
                start_offset=end,
                end_offset=min(len(raw_snippet), end + max_chars),
            )
        result = {
            "found": True,
            "evidence": payload,
            "page_ref": current_page_ref,
            "next_page_ref": next_page_ref,
            "exact_read_complete": exact_read_complete,
        }
        if not exact_read_complete:
            result["next_action"] = (
                "Call read_evidence again with next_page_ref as page_ref, then keep following each returned "
                "next_page_ref until exact_read_complete=true."
                if next_page_ref
                else (
                    "Call read_evidence with page_ref='' and follow each returned next_page_ref until "
                    "exact_read_complete=true."
                )
            )
    else:
        payload = await owner._evidence_detail_payload(item, max_snippet_chars=max_chars)
        if payload.get("error"):
            return {"found": False, **payload}
        marker = getattr(owner, "_mark_evidence_exact_read", None)
        if callable(marker):
            await marker(item, method="read_evidence")
        result = {"found": True, "evidence": payload, "exact_read_complete": True}
    recorder = getattr(owner, "_record_triage_exact_read", None)
    exact_read_control = (
        await recorder(source_ref=source_ref, evidence_ids=[str(evidence_id)])
        if callable(recorder)
        else None
    )
    if exact_read_control:
        result["exact_read_control"] = exact_read_control
    if getattr(owner, "research_current_job_id", None):
        read_ids = set(getattr(owner, "_research_read_evidence_ids", set()) or set())
        read_ids.add(str(evidence_id))
        setattr(owner, "_research_read_evidence_ids", read_ids)
    return result


async def find_quotes_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    requested_refs = [str(item) for item in args.get("source_refs") or [] if str(item).strip()]
    guard_error = await target_aware_triage_read_guard(
        owner,
        target_source_refs=requested_refs,
        exact_read=False,
    )
    if guard_error:
        return {**guard_error, "results": []}
    source_refs = [str(item) for item in args.get("source_refs") or [] if item]
    has_source_scope = (
        getattr(owner, "triage_allowed_source_refs", None) is not None
        or getattr(owner, "research_allowed_source_refs", None) is not None
    )
    if has_source_scope and not source_refs:
        return {
            "accepted": False,
            "error": "source_refs_required_in_current_scope",
            "message": "This scoped agent must pass source_refs from the current source set or investigation job.",
            "results": [],
        }
    scope_error = await owner._triage_scope_error_for_refs(source_refs)
    if scope_error:
        return {**scope_error, "results": []}
    source_ids, invalid = await owner._source_ids_from_refs_or_ids(source_refs)
    if invalid:
        return {
            "accepted": False,
            "error": "source_refs include values outside this run corpus view",
            "invalid_source_refs": invalid,
            "results": [],
        }
    result = await owner.search_corpus(
        query=str(args.get("topic") or ""),
        purpose="quote_discovery",
        source_ids=source_ids or [str(item) for item in args.get("source_ids") or [] if item],
        limit=int(args.get("limit") or 5),
        scope="selected_sources" if source_ids else "all_sources",
    )
    result["results"] = [item for item in result["results"] if len(str(item.get("short_snippet") or "")) >= 40]
    return result


async def search_sources_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "")
    limit = max(1, min(int(args.get("limit") or 8), 8))
    evidence = await owner.search_corpus(
        query=query, purpose="source_map", source_ids=[], limit=limit, scope="all_sources"
    )
    sources = await _source_summaries(owner.db, owner.run_view, evidence["results"], limit=limit)
    return {
        "accepted": True,
        "query": query[:300],
        "returned_source_count": len(sources),
        "returned_evidence_count": len(evidence["results"]),
        "sources": sources,
        "evidence": evidence["results"][:limit],
    }


async def compare_source_evidence_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    requested_refs = [str(item) for item in args.get("source_refs") or [] if str(item).strip()]
    guard_error = await target_aware_triage_read_guard(
        owner,
        target_source_refs=requested_refs,
        exact_read=False,
    )
    if guard_error:
        return {**guard_error, "comparisons": []}
    requested_refs = [str(item) for item in args.get("source_refs") or [] if item]
    scope_error = await owner._triage_scope_error_for_refs(requested_refs)
    if scope_error:
        return {**scope_error, "comparisons": []}
    requested_source_ids, invalid = await owner._source_ids_from_refs_or_ids(requested_refs)
    if invalid:
        return {
            "accepted": False,
            "error": "source_refs include values outside this run corpus view",
            "invalid_source_refs": invalid,
            "comparisons": [],
        }
    if len(requested_source_ids) < 2:
        return {
            "accepted": False,
            "error": "compare_source_evidence requires at least two source_refs",
            "comparisons": [],
        }
    source_ids = requested_source_ids[:5]
    question = str(args.get("question") or "")
    limit_per_source = max(1, min(int(args.get("limit_per_source") or 2), 3))
    comparisons = []
    for source_id in source_ids:
        result = await owner.search_corpus(
            query=question,
            purpose="source_compare",
            source_ids=[source_id],
            limit=limit_per_source,
            scope="selected_sources",
        )
        comparisons.append(
            {"source_ref": await owner._source_ref_for_id(source_id), "evidence": result.get("results", [])}
        )
    await save_tool_trace(
        owner.db,
        generation_run_id=owner.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.COMPARE_SOURCE_EVIDENCE,
        input_json={"source_ids": source_ids, "question": question, "limit_per_source": limit_per_source},
        output_json={
            "evidence_ids": [
                item["evidence_id"]
                for comparison in comparisons
                for item in comparison["evidence"]
                if item.get("evidence_id")
            ]
        },
    )
    return {
        "accepted": True,
        "question": question[:300],
        "comparisons": comparisons,
        "requested_source_count": len(requested_source_ids),
        "compared_source_count": len(source_ids),
        "omitted_source_count": max(0, len(requested_source_ids) - len(source_ids)),
        "expected_next_action": (
            "Use the compared evidence to update finding links or relations, refresh affected outcomes, "
            "or persist a supported source relationship, cluster, or gap before another comparison."
        ),
    }


async def canonical_kinds_by_source_action(owner: Any) -> dict[str, str]:
    canonical_ids = [
        uuid.UUID(item)
        for item in _active_filter_list(owner.run_view, "canonical_source_version_ids")
        if _uuid_or_none(item)
    ]
    if not canonical_ids:
        return {}
    result = await owner.db.execute(select(SourceVersion).where(SourceVersion.id.in_(canonical_ids)))
    return {str(version.source_id): str(version.kind) for version in result.scalars()}


__all__ = [
    "canonical_kinds_by_source_action",
    "compare_source_evidence_tool_action",
    "find_quotes_tool_action",
    "list_sources_tool_action",
    "read_evidence_tool_action",
    "search_corpus_tool_action",
    "search_sources_tool_action",
]
