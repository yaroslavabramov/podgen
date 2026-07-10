from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_refs import source_ref_for_id
from app.generation_v2.tools.evidence_source_map import _source_summaries
from app.generation_v2.tools.evidence_tool_payloads import (
    active_filter_list as _active_filter_list,
    compact_source_card as _compact_source_card,
)
from app.generation_v2.tools.evidence_utils import _uuid_or_none
from app.models.generation_v2 import EvidenceItem, SourceVersion


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
    guard_error = await owner._triage_read_guard_error()
    if guard_error:
        return {**guard_error, "results": []}
    query = str(args.get("query") or "").strip()
    if len(query) < 3:
        return {"accepted": False, "error": "query must contain at least 3 non-whitespace characters", "results": []}
    scope = str(args.get("scope") or "").strip() or ("selected_sources" if args.get("source_refs") else "all_sources")
    if scope not in {"all_sources", "selected_sources"}:
        return {"accepted": False, "error": "scope must be all_sources or selected_sources", "results": []}
    source_refs = [str(item) for item in args.get("source_refs") or [] if item]
    if getattr(owner, "triage_allowed_source_refs", None) and scope == "all_sources":
        return {
            "accepted": False,
            "error": "all_sources_search_not_allowed_in_current_triage_set",
            "message": "Search only within source_refs from the current source set.",
            "allowed_source_refs": sorted(getattr(owner, "triage_allowed_source_refs", set()) or []),
            "results": [],
        }
    if getattr(owner, "research_allowed_source_refs", None) and scope == "all_sources":
        return {
            "accepted": False,
            "error": "all_sources_search_not_allowed_in_current_research_job",
            "message": "Search only within source_refs from the current investigation job.",
            "allowed_source_refs": sorted(getattr(owner, "research_allowed_source_refs", set()) or []),
            "results": [],
        }
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
    guard_error = await owner._triage_read_guard_error()
    if guard_error:
        return guard_error
    try:
        evidence_id = uuid.UUID(str(args.get("evidence_id") or ""))
    except ValueError:
        return {"found": False, "error": "Invalid evidence_id"}
    item = await owner.db.get(EvidenceItem, evidence_id)
    if item is None or item.generation_run_id != owner.run.id:
        return {"found": False}
    scope_error = await owner._triage_scope_error_for_source_ids([str(item.source_id)])
    if scope_error:
        return scope_error
    payload = await owner._evidence_detail_payload(item, max_snippet_chars=2400)
    if payload.get("error"):
        return {"found": False, **payload}
    return {"found": True, "evidence": payload}


async def find_quotes_tool_action(owner: Any, args: dict[str, Any]) -> dict[str, Any]:
    guard_error = await owner._triage_read_guard_error()
    if guard_error:
        return {**guard_error, "results": []}
    source_refs = [str(item) for item in args.get("source_refs") or [] if item]
    if (getattr(owner, "triage_allowed_source_refs", None) or getattr(owner, "research_allowed_source_refs", None)) and not source_refs:
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
    evidence = await owner.search_corpus(query=query, purpose="source_map", source_ids=[], limit=limit, scope="all_sources")
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
    guard_error = await owner._triage_read_guard_error()
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
        return {"accepted": False, "error": "compare_source_evidence requires at least two source_refs", "comparisons": []}
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
        comparisons.append({"source_ref": await owner._source_ref_for_id(source_id), "evidence": result.get("results", [])})
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
        "expected_next_action": "record_triage_observation or write a source cluster/relationship/gap for this source set before starting another comparison.",
    }


async def canonical_kinds_by_source_action(owner: Any) -> dict[str, str]:
    canonical_ids = [uuid.UUID(item) for item in _active_filter_list(owner.run_view, "canonical_source_version_ids") if _uuid_or_none(item)]
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
