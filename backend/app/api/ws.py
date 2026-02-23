import asyncio
import json
import uuid

import jwt
import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models.generation_run import GenerationRun
from app.models.project import Project

router = APIRouter(tags=["websocket"])


async def _verify_ws_token(token: str) -> str | None:
    """Verify JWT and return user_id, or None if invalid."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


async def _verify_run_ownership(run_id: str, user_id: str) -> bool:
    """Check that the generation run belongs to the user."""
    async with async_session_factory() as db:
        result = await db.execute(
            select(GenerationRun)
            .join(Project, GenerationRun.project_id == Project.id)
            .where(
                GenerationRun.id == uuid.UUID(run_id),
                Project.user_id == uuid.UUID(user_id),
            )
        )
        return result.scalar_one_or_none() is not None


@router.websocket("/api/ws/generations/{run_id}")
async def generation_websocket(websocket: WebSocket, run_id: str, token: str = Query(default="")):
    # Verify JWT from query parameter
    user_id = await _verify_ws_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # Verify ownership
    if not await _verify_run_ownership(run_id, user_id):
        await websocket.close(code=4003, reason="Forbidden")
        return

    await websocket.accept()

    r = aioredis.from_url(settings.redis_url)
    pubsub = r.pubsub()
    channel = f"generation:{run_id}"
    await pubsub.subscribe(channel)

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await websocket.send_text(data)

            # Check if client disconnected
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await r.close()
