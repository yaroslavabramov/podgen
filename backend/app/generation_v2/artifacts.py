from __future__ import annotations

import uuid
import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ToolTrace


def payload_from_model(payload: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return payload


async def save_artifact(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    artifact_type: str,
    artifact_key: str,
    payload: BaseModel | dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
    status: str = V2ArtifactStatus.DRAFT,
    parent_artifact_ids: list[str] | None = None,
    input_artifact_ids: list[str] | None = None,
    prompt: str | None = None,
    prompt_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    attempt: int = 1,
) -> V2Artifact:
    version_result = await db.execute(
        select(func.max(V2Artifact.version)).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == artifact_type,
            V2Artifact.artifact_key == artifact_key,
        )
    )
    next_version = int(version_result.scalar() or 0) + 1
    previous_result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == artifact_type,
            V2Artifact.artifact_key == artifact_key,
            V2Artifact.is_current.is_(True),
        )
    )
    previous_current = list(previous_result.scalars())
    payload_json = payload_from_model(payload)
    if metadata:
        payload_json = {**payload_json, "_metadata": {**(payload_json.get("_metadata") or {}), **metadata}}
    artifact = V2Artifact(
        generation_run_id=generation_run_id,
        artifact_type=artifact_type,
        artifact_key=artifact_key,
        version=next_version,
        attempt=attempt,
        is_current=True,
        prompt_hash=prompt_hash or (hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt else None),
        input_artifact_ids=input_artifact_ids or [],
        parent_artifact_ids=parent_artifact_ids or [],
        payload=payload_json,
        provider=provider,
        model=model,
        status=status,
    )
    db.add(artifact)
    await db.flush()
    if previous_current:
        await db.execute(
            update(V2Artifact)
            .where(V2Artifact.id.in_([item.id for item in previous_current]))
            .values(is_current=False, status=V2ArtifactStatus.SUPERSEDED, superseded_by_id=artifact.id)
        )
        await db.flush()
    return artifact


async def list_artifacts_grouped(
    db: AsyncSession,
    generation_run_id: uuid.UUID,
    *,
    include_versions: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    query = select(V2Artifact).where(V2Artifact.generation_run_id == generation_run_id)
    if not include_versions:
        query = query.where(V2Artifact.is_current.is_(True))
    result = await db.execute(
        query.order_by(V2Artifact.artifact_type.asc(), V2Artifact.artifact_key.asc(), V2Artifact.version.asc())
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for artifact in result.scalars():
        grouped.setdefault(artifact.artifact_type, []).append(
            {
                "id": str(artifact.id),
                "artifact_key": artifact.artifact_key,
                "version": artifact.version,
                "attempt": artifact.attempt,
                "is_current": artifact.is_current,
                "prompt_hash": artifact.prompt_hash,
                "input_artifact_ids": artifact.input_artifact_ids,
                "superseded_by_id": str(artifact.superseded_by_id) if artifact.superseded_by_id else None,
                "status": artifact.status,
                "provider": artifact.provider,
                "model": artifact.model,
                "created_at": artifact.created_at.isoformat() if isinstance(artifact.created_at, datetime) else None,
                "payload": artifact.payload,
            }
        )
    return grouped


async def save_tool_trace(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    tool_name: str,
    input_json: dict[str, Any],
    output_json: dict[str, Any],
    provider_call_id: str | None = None,
    latency_ms: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> V2ToolTrace:
    trace = V2ToolTrace(
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        tool_name=tool_name,
        input_json=input_json,
        output_json=output_json,
        provider_call_id=provider_call_id,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    db.add(trace)
    await db.flush()
    return trace


async def list_tool_traces(db: AsyncSession, generation_run_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db.execute(
        select(V2ToolTrace)
        .where(V2ToolTrace.generation_run_id == generation_run_id)
        .order_by(V2ToolTrace.created_at.asc())
    )
    traces = []
    for trace in result.scalars():
        traces.append(
            {
                "id": str(trace.id),
                "phase": trace.phase,
                "agent_name": trace.agent_name,
                "tool_name": trace.tool_name,
                "input_json": trace.input_json,
                "output_json": trace.output_json,
                "provider_call_id": trace.provider_call_id,
                "latency_ms": trace.latency_ms,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "cost_usd": trace.cost_usd,
                "created_at": trace.created_at.isoformat() if isinstance(trace.created_at, datetime) else None,
            }
        )
    return traces
