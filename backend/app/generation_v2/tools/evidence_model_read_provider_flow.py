from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_tool_payloads import (
    cached_model_read_payload as _cached_model_read_payload,
    model_read_agent_payload as _model_read_agent_payload,
)
from app.generation_v2.tools.evidence_utils import _model_file_read_snippet
from app.models.generation_v2 import ProviderCorpusFile, SourceVersion


async def try_provider_model_read(
    owner: Any,
    *,
    candidates: list[tuple[ProviderCorpusFile, SourceVersion]],
    source_id: str,
    source_version_id: str,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
    request_cache_key: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    del source_id, source_version_id
    provider_failures: list[dict[str, Any]] = []
    for provider_file, version in candidates:
        cache_key = owner._model_read_cache_key(
            source_version_id=str(version.id),
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            read_surface_id=str(provider_file.provider_file_id or ""),
        )
        cached_items = await owner._cached_evidence_items(cache_field="model_read_cache_key", cache_key=cache_key, limit=1)
        if cached_items:
            cached = cached_items[0]
            await owner._mark_evidence_exact_read(cached, method="read_source_with_model")
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
                },
                output_json={
                    "cache_hit": True,
                    "evidence_id": payload["evidence_id"],
                    "support_level": (cached.metadata_ or {}).get("support_level"),
                },
                provider_call_id=cached.response_id,
            )
            return {
                "found": True,
                "cache_hit": True,
                "evidence": payload,
                "model_read": _cached_model_read_payload(cached),
            }, provider_failures
        try:
            read_result, response_id = await owner._model_read_provider_file(
                provider_file=provider_file,
                version=version,
                focus_question=focus_question,
                claim_text=claim_text,
                purpose=purpose,
                locator_hint=locator_hint,
            )
        except Exception as exc:
            failure = {
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "error": str(exc),
            }
            provider_failures.append(failure)
            await owner._save_model_read_failure_trace(
                provider_file=provider_file,
                version=version,
                purpose=purpose,
                focus_question=focus_question,
                claim_text=claim_text,
                locator_hint=locator_hint,
                error=str(exc),
            )
            continue

        snippet_text = _model_file_read_snippet(read_result)
        if not snippet_text.strip():
            provider_failures.append(
                {
                    "source_id": str(version.source_id),
                    "source_version_id": str(version.id),
                    "error": "Model file read returned no quote, snippet, or support summary",
                }
            )
            continue

        evidence = await owner._persist_model_read_evidence(
            provider_file=provider_file,
            version=version,
            read_result=read_result,
            response_id=response_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            cache_key=cache_key,
            request_cache_key=request_cache_key,
        )
        await owner._mark_evidence_exact_read(evidence, method="read_source_with_model")
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
                "provider_file_id": provider_file.provider_file_id,
                "purpose": purpose,
                "focus_question": focus_question,
                "claim_text": claim_text,
                "locator_hint": locator_hint,
                "candidate_count": len(candidates),
                "prior_failure_count": len(provider_failures),
                "cache_key": cache_key,
                "request_cache_key": request_cache_key,
            },
            output_json={
                "cache_hit": False,
                "evidence_id": payload["evidence_id"],
                "support_level": read_result.get("support_level"),
            },
            provider_call_id=response_id,
        )
        return {"found": True, "evidence": payload, "model_read": _model_read_agent_payload(read_result)}, provider_failures
    return None, provider_failures


__all__ = ["try_provider_model_read"]
