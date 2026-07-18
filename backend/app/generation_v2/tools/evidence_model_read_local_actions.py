from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_tool_payloads import coverage_warning as _coverage_warning
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_OBSERVED
from app.generation_v2.tools.evidence_utils import _normalize
from app.models.generation_v2 import EvidenceRetrievalChannel, EvidenceStrength


async def persist_local_model_read_fallback(
    owner: Any,
    *,
    version: Any,
    snippet: str,
    locator_text: str,
    locator_type: str,
    channel: str,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
    cache_key: str,
    request_cache_key: str,
    provider_candidate_count: int,
    provider_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = await persist_evidence_item(
        owner.db,
        generation_run_id=owner.run.id,
        project_id=owner.run.project_id,
        source_id=version.source_id,
        source_version_id=version.id,
        provider=None,
        retrieval_channel=EvidenceRetrievalChannel.MODEL_FILE_READ_LOCAL_FALLBACK,
        query_text=focus_question or claim_text,
        purpose=purpose,
        search_result_index=0,
        score=0.65,
        snippet_text=snippet,
        normalized_quote_text=_normalize(snippet),
        locator_text=locator_text or "local source window",
        locator_type=locator_type,
        evidence_strength=EvidenceStrength.WEAK,
        metadata={
            "usage_status": EVIDENCE_STATUS_OBSERVED,
            "model_file_read": False,
            "model_file_read_local_fallback": True,
            "local_read_channel": channel,
            "source_version_kind": version.kind,
            "support_summary": "Local fallback returned a focused source window after provider model-read was unavailable or failed.",
            "locator_hint": locator_hint,
            "provider_candidate_count": provider_candidate_count,
            "provider_failures": provider_failures,
            "model_read_cache_key": cache_key,
            "model_read_request_cache_key": request_cache_key,
        },
    )
    await owner._mark_evidence_exact_read(evidence, method="read_source_with_model_local_fallback")
    payload = await owner._evidence_detail_payload(evidence, max_snippet_chars=2400)
    await save_tool_trace(
        owner.db,
        generation_run_id=owner.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.READ_SOURCE_WITH_MODEL,
        input_json={
            "source_id": str(version.source_id),
            "source_version_id": str(version.id),
            "purpose": purpose,
            "focus_question": focus_question,
            "claim_text": claim_text,
            "locator_hint": locator_hint,
            "provider_candidate_count": provider_candidate_count,
            "cache_key": cache_key,
            "request_cache_key": request_cache_key,
        },
        output_json={
            "cache_hit": False,
            "evidence_id": payload["evidence_id"],
            "local_fallback": True,
            "local_read_channel": channel,
            "provider_failures": provider_failures,
        },
    )
    return {
        "found": True,
        "evidence": payload,
        "coverage_warning": _coverage_warning(evidence),
        "provider_failure_count": len(provider_failures),
    }


async def cached_local_model_read_response(
    owner: Any,
    cached: Any,
    *,
    version: Any,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
    cache_key: str,
    request_cache_key: str,
    provider_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    await owner._mark_evidence_exact_read(cached, method="read_source_with_model_local_fallback")
    payload = await owner._evidence_detail_payload(cached, max_snippet_chars=2400)
    await save_tool_trace(
        owner.db,
        generation_run_id=owner.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.READ_SOURCE_WITH_MODEL,
        input_json={
            "source_id": str(version.source_id),
            "source_version_id": str(version.id),
            "purpose": purpose,
            "focus_question": focus_question,
            "claim_text": claim_text,
            "locator_hint": locator_hint,
            "cache_key": cache_key,
            "request_cache_key": request_cache_key,
        },
        output_json={"cache_hit": True, "evidence_id": payload["evidence_id"], "local_fallback": True},
        provider_call_id=cached.response_id,
    )
    return {
        "found": True,
        "cache_hit": True,
        "evidence": payload,
        "coverage_warning": _coverage_warning(cached),
        "provider_failure_count": len(provider_failures),
    }


async def save_empty_local_model_read_trace(
    owner: Any,
    *,
    version: Any,
    purpose: str,
    focus_question: str,
    claim_text: str,
    locator_hint: str,
    provider_candidate_count: int,
    provider_failures: list[dict[str, Any]],
) -> None:
    await save_tool_trace(
        owner.db,
        generation_run_id=owner.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.READ_SOURCE_WITH_MODEL_LOCAL_FALLBACK,
        input_json={
            "source_id": str(version.source_id),
            "source_version_id": str(version.id),
            "purpose": purpose,
            "focus_question": focus_question,
            "claim_text": claim_text,
            "locator_hint": locator_hint,
            "provider_candidate_count": provider_candidate_count,
        },
        output_json={"found": False, "provider_failures": provider_failures},
    )


__all__ = [
    "cached_local_model_read_response",
    "persist_local_model_read_fallback",
    "save_empty_local_model_read_trace",
]
