import uuid
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class SourceURLCreate(BaseModel):
    url: str = Field(..., min_length=1)
    source_type: str = Field(default="web", pattern=r"^(web|video|audio)$")


class SourceResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_type: str
    original_url: str | None
    file_path: str | None
    title: str | None
    published_at: datetime | None
    domain_reliability: str
    ingestion_status: str
    is_temporary: bool
    fully_ingested: bool
    text_preview: str | None
    metadata_: dict | None = Field(
        default=None,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class SourceListResponse(BaseModel):
    items: list[SourceResponse]
    total: int
