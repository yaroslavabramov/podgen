from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import asyncio
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.source_workspace_provider_polling import (
    commit_if_supported,
    poll_workspace_provider_file,
    workspace_provider_operation_timeout_seconds,
    workspace_retrieve_vector_store_file,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import SourceRepresentation, SourceRepresentationKind, V2RecordStatus
from app.models.source import Source
from app.services.llm import get_openai_client


async def upload_provider_file(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    filename: str,
    content: bytes,
    vector_store_id: str,
) -> None:
    existing_result = await db.execute(
        select(SourceRepresentation).where(
            SourceRepresentation.source_id == source.id,
            SourceRepresentation.kind == SourceRepresentationKind.PROVIDER_FILE,
            SourceRepresentation.provider == "openai",
            SourceRepresentation.provider_vector_store_id == vector_store_id,
        )
    )
    provider_rep = next(
        (
            item
            for item in existing_result.scalars()
            if str((item.metadata_ or {}).get("source_representation_id") or "") == str(rep.id)
        ),
        None,
    )
    if provider_rep is None:
        from app.generation_v2.source_workspace_representations import save_representation

        provider_rep = await save_representation(
            db,
            source=source,
            kind=SourceRepresentationKind.PROVIDER_FILE,
            provider="openai",
            provider_vector_store_id=vector_store_id,
            status=V2RecordStatus.PENDING,
            content=content,
            metadata={
                "source_representation_id": str(rep.id),
                "filename": filename,
                "upload_phase": "db_prepared",
            },
        )
        await commit_if_supported(db)
    client = get_openai_client()
    if not provider_rep.provider_file_id:
        file_obj = await asyncio.wait_for(
            client.files.create(file=(filename, io.BytesIO(content)), purpose="assistants"),
            timeout=max(30, int(settings.generation_v2_provider_upload_timeout_seconds)),
        )
        provider_rep.provider_file_id = file_obj.id
        provider_rep.status = V2RecordStatus.PENDING
        provider_rep.metadata_ = {
            **(provider_rep.metadata_ or {}),
            "upload_phase": "file_uploaded",
        }
        await db.flush()
        await commit_if_supported(db)
    vector_file = await workspace_retrieve_vector_store_file(client, vector_store_id, provider_rep.provider_file_id)
    if vector_file is None:
        vector_file = await asyncio.wait_for(
            client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=provider_rep.provider_file_id,
                attributes={"source_id": str(source.id), "representation_id": str(rep.id), "kind": rep.kind},
            ),
            timeout=workspace_provider_operation_timeout_seconds(),
        )
    provider_rep.metadata_ = {
        **(provider_rep.metadata_ or {}),
        "upload_phase": "attached",
        "provider_vector_store_file_id": getattr(vector_file, "id", None),
        "last_provider_status": getattr(vector_file, "status", None),
    }
    await db.flush()
    await commit_if_supported(db)
    await poll_workspace_provider_file(
        db,
        client,
        provider_rep,
        vector_store_id,
    )
    await save_tool_trace(
        db,
        generation_run_id=run.id,
        phase=V2Phase.SOURCE_WORKSPACE,
        agent_name=V2AgentName.SOURCE_WORKSPACE,
        tool_name=V2ToolName.OPENAI_FILES_CREATE_VECTOR_STORE_FILE,
        input_json={"source_id": str(source.id), "representation_id": str(rep.id), "filename": filename},
        output_json={
            "provider_file_id": provider_rep.provider_file_id,
            "provider_vector_store_file_id": (provider_rep.metadata_ or {}).get("provider_vector_store_file_id"),
            "provider_vector_store_file_status": (provider_rep.metadata_ or {}).get("last_provider_status"),
            "provider_representation_id": str(provider_rep.id),
        },
        provider_call_id=provider_rep.provider_file_id,
    )


__all__ = [
    "poll_workspace_provider_file",
    "upload_provider_file",
    "workspace_provider_operation_timeout_seconds",
    "workspace_retrieve_vector_store_file",
]
