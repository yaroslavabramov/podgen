from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import uuid
from typing import Any

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.tools.evidence_model_read_local import EvidenceModelReadLocalMixin
from app.generation_v2.tools.evidence_model_read_persistence import EvidenceModelReadPersistenceMixin
from app.generation_v2.tools.evidence_model_read_provider_flow import try_provider_model_read as _try_provider_model_read
from app.generation_v2.tools.evidence_tool_payloads import (
    cached_model_read_payload as _cached_model_read_payload,
    coverage_warning as _coverage_warning,
)
from app.generation_v2.tools.evidence_utils import (
    _dedupe,
    _uuid_or_none,
)
from app.models.generation_v2 import (
    EvidenceRetrievalChannel,
    ProviderCorpusFile,
    SourceVersion,
)


class EvidenceModelReadMixin(EvidenceModelReadLocalMixin, EvidenceModelReadPersistenceMixin):
    async def read_source_with_model_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        focus_question = str(args.get("focus_question") or "").strip()
        claim_text = str(args.get("claim_text") or "").strip()
        purpose = str(args.get("purpose") or "model_file_read").strip() or "model_file_read"
        locator_hint = str(args.get("locator_hint") or "").strip()
        if not (focus_question or claim_text):
            return {"found": False, "error": "focus_question or claim_text is required"}

        source_id = str(args.get("source_id") or "")
        source_version_id = str(args.get("source_version_id") or "")
        source_ref = str(args.get("source_ref") or "")
        version_ref = str(args.get("version_ref") or "")
        if source_ref and not source_id:
            resolved_source_id = await self._source_id_from_ref_or_id(source_ref)
            if not resolved_source_id:
                return {
                    "found": False,
                    "accepted": False,
                    "error": "source_ref is outside this run corpus view",
                    "invalid_source_ref": source_ref,
                }
            source_id = resolved_source_id
        if version_ref and not source_version_id:
            resolved_source_version_id = await self._source_version_id_from_ref(version_ref)
            if not resolved_source_version_id:
                return {
                    "found": False,
                    "accepted": False,
                    "error": "version_ref is outside this run corpus view",
                    "invalid_version_ref": version_ref,
                }
            source_version_id = resolved_source_version_id
        scope_source_ids = [source_id] if source_id and _uuid_or_none(source_id) else []
        if source_version_id and _uuid_or_none(source_version_id):
            scoped_version = await self.db.get(SourceVersion, uuid.UUID(source_version_id))
            if scoped_version is not None:
                scope_source_ids.append(str(scoped_version.source_id))
        scope_error = await self._triage_scope_error_for_source_ids(_dedupe(scope_source_ids))
        if scope_error:
            return {**scope_error, "found": False}
        request_cache_key = self._model_read_request_cache_key(
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
        )
        cached_request_items = await self._cached_evidence_items(cache_field="model_read_request_cache_key", cache_key=request_cache_key, limit=1)
        if cached_request_items:
            cached = cached_request_items[0]
            payload = await self._evidence_detail_payload(cached, max_snippet_chars=2400)
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase=V2Phase.CORPUS_EVIDENCE,
                agent_name=V2AgentName.CORPUS_EVIDENCE_API,
                tool_name=V2ToolName.READ_SOURCE_WITH_MODEL,
                input_json={
                    "source_id": source_id,
                    "source_version_id": source_version_id,
                    "purpose": purpose,
                    "focus_question": focus_question,
                    "claim_text": claim_text,
                    "locator_hint": locator_hint,
                    "request_cache_key": request_cache_key,
                },
                output_json={"cache_hit": True, "evidence_id": payload["evidence_id"], "support_level": (cached.metadata_ or {}).get("support_level")},
                provider_call_id=cached.response_id,
            )
            response: dict[str, Any] = {
                "found": True,
                "cache_hit": True,
                "evidence": payload,
            }
            if cached.retrieval_channel == EvidenceRetrievalChannel.MODEL_FILE_READ_LOCAL_FALLBACK:
                response["coverage_warning"] = _coverage_warning(cached)
            else:
                response["model_read"] = _cached_model_read_payload(cached)
            return response
        candidates: list[tuple[ProviderCorpusFile, SourceVersion]] = []
        if settings.openai_api_key:
            candidates = await self._resolve_provider_file_candidates_for_model_read(
                source_id=source_id,
                source_version_id=source_version_id,
            )
        provider_result, provider_failures = await _try_provider_model_read(
            self,
            candidates=candidates,
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            request_cache_key=request_cache_key,
        )
        if provider_result is not None:
            return provider_result

        return await self._model_read_local_fallback(
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            request_cache_key=request_cache_key,
            provider_failures=provider_failures,
            provider_candidate_count=len(candidates),
        )

__all__ = ["EvidenceModelReadMixin"]
