import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    language: str = Field(default="en", pattern=r"^(en|ru)$")


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    language: str | None = Field(None, pattern=r"^(en|ru)$")


class ProjectResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    description: str | None
    language: str
    created_at: datetime
    updated_at: datetime
    source_count: int = 0
    episode_count: int = 0

    model_config = {"from_attributes": True}


class ProjectDetailResponse(ProjectResponse):
    """Extended response for GET /api/projects/{id} — includes sources and show_bible."""
    sources: list = []
    show_bible: dict | None = None


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    total: int
