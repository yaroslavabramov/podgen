import uuid

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, DB
from app.models.episode import Episode
from app.models.project import Project
from app.schemas.episode import EpisodeResponse, EpisodeListResponse
from app.services.storage import get_minio_client, download_file

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


@router.get("", response_model=EpisodeListResponse)
async def list_episodes(user: CurrentUser, db: DB):
    result = await db.execute(
        select(Episode)
        .join(Project, Episode.project_id == Project.id)
        .where(Project.user_id == user.id)
        .order_by(Episode.created_at.desc())
    )
    episodes = result.scalars().all()
    return EpisodeListResponse(items=[EpisodeResponse.model_validate(e) for e in episodes], total=len(episodes))


@router.get("/{episode_id}", response_model=EpisodeResponse)
async def get_episode(episode_id: uuid.UUID, user: CurrentUser, db: DB):
    episode = await _get_user_episode(episode_id, user.id, db)
    return EpisodeResponse.model_validate(episode)


@router.get("/{episode_id}/audio")
async def stream_audio(episode_id: uuid.UUID, user: CurrentUser, db: DB):
    episode = await _get_user_episode(episode_id, user.id, db)
    if not episode.audio_file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio not available")

    client = get_minio_client()
    data = download_file(client, episode.audio_file_path)

    import io
    return StreamingResponse(io.BytesIO(data), media_type="audio/mpeg", headers={
        "Content-Disposition": f'inline; filename="episode_{episode_id}.mp3"',
        "Content-Length": str(len(data)),
    })


@router.get("/{episode_id}/transcript")
async def get_transcript(episode_id: uuid.UUID, user: CurrentUser, db: DB):
    episode = await _get_user_episode(episode_id, user.id, db)
    return episode.transcript_json or []


@router.get("/{episode_id}/shownotes")
async def get_shownotes(episode_id: uuid.UUID, user: CurrentUser, db: DB):
    episode = await _get_user_episode(episode_id, user.id, db)
    return episode.shownotes_json or {}


@router.delete("/{episode_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_episode(episode_id: uuid.UUID, user: CurrentUser, db: DB):
    episode = await _get_user_episode(episode_id, user.id, db)
    await db.delete(episode)


async def _get_user_episode(episode_id: uuid.UUID, user_id: uuid.UUID, db) -> Episode:
    result = await db.execute(
        select(Episode)
        .join(Project, Episode.project_id == Project.id)
        .where(Episode.id == episode_id, Project.user_id == user_id)
    )
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
    return episode
