import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GenerationCreate(BaseModel):
    duration_target: int = Field(..., ge=900, le=5400, description="Target duration in seconds")
    user_prompt: str = Field(..., min_length=1, max_length=5000)
    show_bible_id: uuid.UUID | None = None
    show_bible_override: dict | None = None
    voice_a_id: str | None = None
    voice_b_id: str | None = None


class CostEstimateRequest(BaseModel):
    duration_target: int = Field(..., ge=900, le=5400)


class CostEstimateResponse(BaseModel):
    estimated_llm_cost: float
    estimated_tts_cost: float
    estimated_total: float
    estimated_minutes: float


class GenerationRunResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    show_bible_id: uuid.UUID
    status: str
    config: dict
    current_phase: str | None
    current_step: str | None
    progress_pct: float | None
    estimated_cost: dict | None
    actual_cost: dict | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
