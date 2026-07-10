from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import io

from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_v2 import ProviderCorpusFile, SourceVersion


async def ensure_model_read_file_id(toolset, provider_file: ProviderCorpusFile, version: SourceVersion) -> str:
    attributes = provider_file.attributes or {}
    model_read_file_id = str(attributes.get("model_read_file_id") or "")
    if model_read_file_id:
        return model_read_file_id
    from app.generation_v2.tools import evidence_retrieval

    filename, content = evidence_retrieval._model_read_upload_payload(version)
    if not content:
        raise RuntimeError("Source version content is empty; cannot upload model-read input file")
    client = evidence_retrieval.get_openai_client()
    file_obj = await client.files.create(file=(filename, io.BytesIO(content)), purpose="user_data")
    model_read_file_id = str(file_obj.id)
    provider_file.attributes = {
        **attributes,
        "model_read_file_id": model_read_file_id,
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
        input_json={"source_version_id": str(version.id), "filename": filename, "purpose": "user_data"},
        output_json={"model_read_file_id": model_read_file_id},
        provider_call_id=model_read_file_id,
    )
    return model_read_file_id


__all__ = ["ensure_model_read_file_id"]
