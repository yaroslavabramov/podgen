import uuid
from datetime import datetime

from pydantic import BaseModel


class EpisodeResponse(BaseModel):
    id: uuid.UUID
    generation_run_id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str | None
    language: str
    duration_seconds: int
    audio_file_path: str | None
    transcript_json: list | None
    shownotes_json: dict | None
    quality_metrics: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EpisodeListResponse(BaseModel):
    items: list[EpisodeResponse]
    total: int
