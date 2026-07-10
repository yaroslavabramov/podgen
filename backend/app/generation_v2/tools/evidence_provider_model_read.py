from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from typing import Any

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.model_routing import generation_v2_model_read_model
from app.generation_v2.tools.evidence_model_read_candidates import (
    resolve_provider_file_candidates_for_model_read as _resolve_provider_file_candidates_for_model_read,
)
from app.generation_v2.tools.evidence_model_read_upload import ensure_model_read_file_id as _ensure_model_read_file_id
from app.generation_v2.tools.evidence_provider_model_read_actions import (
    model_read_provider_file as _model_read_provider_file_action,
)
from app.models.generation_v2 import ProviderCorpusFile, SourceVersion


class EvidenceProviderModelReadMixin:
    async def _resolve_provider_file_candidates_for_model_read(
        self,
        *,
        source_id: str,
        source_version_id: str,
    ) -> list[tuple[ProviderCorpusFile, SourceVersion]]:
        return await _resolve_provider_file_candidates_for_model_read(
            self,
            source_id=source_id,
            source_version_id=source_version_id,
        )

    async def _save_model_read_failure_trace(
        self,
        *,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
        purpose: str,
        focus_question: str,
        claim_text: str,
        locator_hint: str,
        error: str,
    ) -> None:
        requested_model = generation_v2_model_read_model()
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.CORPUS_EVIDENCE,
            agent_name=V2AgentName.CORPUS_EVIDENCE_API,
            tool_name=V2ToolName.OPENAI_RESPONSES_CREATE_INPUT_FILE,
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "provider_file_id": provider_file.provider_file_id,
                "purpose": purpose,
                "focus_question": focus_question,
                "claim_text": claim_text,
                "locator_hint": locator_hint,
                "requested_model": requested_model,
            },
            output_json={"error": error, "requested_model": requested_model},
        )

    async def _model_read_provider_file(
        self,
        *,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
    ) -> tuple[dict[str, Any], str | None]:
        return await _model_read_provider_file_action(
            self,
            provider_file=provider_file,
            version=version,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
        )

    async def _ensure_model_read_file_id(self, provider_file: ProviderCorpusFile, version: SourceVersion) -> str:
        return await _ensure_model_read_file_id(self, provider_file, version)

__all__ = [
    "_model_read_provider_file_action",
]
