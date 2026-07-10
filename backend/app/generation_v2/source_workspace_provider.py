from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.source_workspace_provider_upload import (
    poll_workspace_provider_file as _poll_workspace_provider_file_impl,
    upload_provider_file as _upload_provider_file_impl,
    workspace_provider_operation_timeout_seconds as _workspace_provider_operation_timeout_seconds_impl,
    workspace_retrieve_vector_store_file as _workspace_retrieve_vector_store_file_impl,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    CorpusWorkspace,
    SourceRepresentation,
)
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import download_file, get_minio_client

logger = logging.getLogger(__name__)


async def cleanup_provider_workspace(db: AsyncSession, run_id: uuid.UUID) -> None:
    if not settings.openai_api_key or not settings.generation_v2_cleanup_provider_files:
        return
    client = get_openai_client()
    workspace_result = await db.execute(
        select(CorpusWorkspace).where(CorpusWorkspace.generation_run_id == run_id, CorpusWorkspace.provider == "openai")
    )
    workspaces = list(workspace_result.scalars())
    vector_store_ids = [workspace.provider_vector_store_id for workspace in workspaces if workspace.provider_vector_store_id]
    rep_result = await db.execute(
        select(SourceRepresentation).where(
            SourceRepresentation.provider == "openai",
            SourceRepresentation.provider_file_id.is_not(None),
            SourceRepresentation.provider_vector_store_id.in_(vector_store_ids or [""]),
        )
    )
    provider_reps = list(rep_result.scalars())
    for rep in provider_reps:
        try:
            await client.files.delete(rep.provider_file_id)
            rep.metadata_ = {**(rep.metadata_ or {}), "provider_cleanup_status": "deleted"}
        except Exception:
            logger.info("OpenAI provider file cleanup failed for %s", rep.provider_file_id, exc_info=True)
    for workspace in workspaces:
        if not workspace.provider_vector_store_id:
            continue
        try:
            await client.vector_stores.delete(workspace.provider_vector_store_id)
            workspace.config = {**(workspace.config or {}), "provider_cleanup": "deleted"}
        except Exception:
            logger.info("OpenAI vector store cleanup failed for %s", workspace.provider_vector_store_id, exc_info=True)
    await db.flush()


async def _maybe_create_vector_store(
    db: AsyncSession,
    run: GenerationRun,
    workspace: CorpusWorkspace,
) -> str | None:
    if not settings.openai_api_key or not settings.generation_v2_enable_provider_uploads:
        return None
    try:
        client = get_openai_client()
        vector_store = await client.vector_stores.create(
            name=f"podgen-v2-{run.project_id}-{run.id}",
            metadata={"project_id": str(run.project_id), "generation_run_id": str(run.id)},
        )
        await save_tool_trace(
            db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_WORKSPACE,
            agent_name=V2AgentName.SOURCE_WORKSPACE,
            tool_name=V2ToolName.OPENAI_VECTOR_STORES_CREATE,
            input_json={"workspace_id": str(workspace.id)},
            output_json={"vector_store_id": vector_store.id},
            provider_call_id=vector_store.id,
        )
        return vector_store.id
    except Exception as exc:
        logger.warning("OpenAI vector store creation failed; continuing without hosted file search", exc_info=True)
        workspace.config = {**(workspace.config or {}), "provider_upload_error": str(exc)}
        return None


async def _maybe_upload_file_representation(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    vector_store_id: str | None,
) -> None:
    if not vector_store_id or not settings.openai_api_key or not source.file_path:
        return
    try:
        content = download_file(get_minio_client(), source.file_path)
        filename = (source.title or source.file_path.rsplit("/", 1)[-1] or f"{source.id}.bin").replace("/", "_")
        await _upload_provider_file(db, run, source, rep, filename, content, vector_store_id)
    except Exception as exc:
        logger.warning("Provider upload failed for source file %s", source.id, exc_info=True)
        rep.error_message = str(exc)


async def _maybe_upload_text_representation(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    text: str,
    vector_store_id: str | None,
) -> None:
    if not vector_store_id or not settings.openai_api_key or not text.strip():
        return
    filename = f"{source.id}-{rep.kind}.txt"
    try:
        await _upload_provider_file(db, run, source, rep, filename, text.encode("utf-8"), vector_store_id)
    except Exception as exc:
        logger.warning("Provider upload failed for source text %s", source.id, exc_info=True)
        rep.error_message = str(exc)


async def _upload_provider_file(
    db: AsyncSession,
    run: GenerationRun,
    source: Source,
    rep: SourceRepresentation,
    filename: str,
    content: bytes,
    vector_store_id: str,
) -> None:
    await _upload_provider_file_impl(db, run, source, rep, filename, content, vector_store_id)


async def _poll_workspace_provider_file(
    db: AsyncSession,
    client: Any,
    provider_rep: SourceRepresentation,
    vector_store_id: str,
) -> None:
    await _poll_workspace_provider_file_impl(db, client, provider_rep, vector_store_id)


async def _workspace_retrieve_vector_store_file(client: Any, vector_store_id: str, provider_file_id: str | None) -> Any | None:
    return await _workspace_retrieve_vector_store_file_impl(client, vector_store_id, provider_file_id)


def _workspace_provider_operation_timeout_seconds() -> int:
    return _workspace_provider_operation_timeout_seconds_impl()


async def _commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()
