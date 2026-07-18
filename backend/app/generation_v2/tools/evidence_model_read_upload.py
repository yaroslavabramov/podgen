from __future__ import annotations

import hashlib
import io
import secrets

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName

from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_v2 import ProviderCorpusFile, SourceVersion


class ModelReadContentIntegrityError(RuntimeError):
    pass


async def ensure_model_read_file_id(toolset, provider_file: ProviderCorpusFile, version: SourceVersion) -> str:
    from app.generation_v2.tools import evidence_retrieval

    attributes = provider_file.attributes or {}
    filename, content = evidence_retrieval._model_read_upload_payload(version)
    if not content:
        raise ModelReadContentIntegrityError(
            "Source version content is empty; cannot upload model-read input file"
        )
    expected_hash = str(version.content_hash or "").strip().lower()
    actual_hash = hashlib.sha256(content).hexdigest()
    if not expected_hash or not secrets.compare_digest(actual_hash, expected_hash):
        raise ModelReadContentIntegrityError(
            "Pinned source version content hash does not match downloaded bytes"
        )

    model_read_file_id = str(attributes.get("model_read_file_id") or "")
    cached_content_hash = str(attributes.get("model_read_content_hash") or "").strip().lower()
    client = evidence_retrieval.get_openai_client()
    if model_read_file_id and secrets.compare_digest(cached_content_hash, actual_hash):
        retrieve = getattr(getattr(client, "files", None), "retrieve", None)
        if not callable(retrieve):
            return model_read_file_id
        try:
            await retrieve(model_read_file_id)
            return model_read_file_id
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404 and type(exc).__name__ != "NotFoundError":
                raise

    file_obj = await client.files.create(file=(filename, io.BytesIO(content)), purpose="user_data")
    model_read_file_id = str(file_obj.id)
    provider_file.attributes = {
        **attributes,
        "model_read_file_id": model_read_file_id,
        "model_read_content_hash": actual_hash,
        "model_read_file_purpose": "user_data",
        "model_read_filename": filename,
        "vector_store_provider_file_id": provider_file.provider_file_id,
    }
    await toolset.db.flush()
    await save_tool_trace(
        toolset.db,
        generation_run_id=toolset.run.id,
        phase=V2Phase.CORPUS_EVIDENCE,
        agent_name=V2AgentName.CORPUS_EVIDENCE_API,
        tool_name=V2ToolName.OPENAI_FILES_CREATE_MODEL_READ,
        input_json={
            "source_version_id": str(version.id),
            "filename": filename,
            "purpose": "user_data",
            "content_sha256": actual_hash,
            "content_bytes": len(content),
        },
        output_json={"uploaded": True, "content_sha256": actual_hash},
    )
    return model_read_file_id


__all__ = ["ModelReadContentIntegrityError", "ensure_model_read_file_id"]
