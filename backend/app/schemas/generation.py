import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GenerationCreate(BaseModel):
    duration_target: int = Field(..., ge=900, le=5400, description="Target duration in seconds")
    user_prompt: str = Field(..., min_length=1, max_length=5000)
    show_bible_id: uuid.UUID | None = None
    show_bible_override: dict | None = None
    voice_a_id: str | None = None
    voice_b_id: str | None = None
    execution_mode: Literal["full", "step"] = "full"
    generation_mode: Literal["v1", "v2", "compare"] = "v1"


class CostEstimateRequest(BaseModel):
    duration_target: int = Field(..., ge=900, le=5400)


class CostEstimateResponse(BaseModel):
    estimated_llm_cost: float
    estimated_tts_cost: float
    estimated_total: float
    estimated_minutes: float


class StepRerunRequest(BaseModel):
    target_node: str | None = None


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


class EpisodePlanActArtifact(BaseModel):
    act_number: int
    story_role: str | None
    act_title: str | None
    purpose: str | None
    internal_arc: str | None
    opening_question: str | None
    ending_turn: str | None


class BeatSheetSceneArtifact(BaseModel):
    scene_number: int
    scene_type: str | None
    scene_job: str | None
    description: str | None
    transition_note: str | None


class BeatSheetActArtifact(BaseModel):
    act_number: int
    story_role: str | None
    act_title: str | None
    scenes: list[BeatSheetSceneArtifact]


class ScreenplayLineArtifact(BaseModel):
    speaker: str
    text: str


class ScreenplaySceneArtifact(BaseModel):
    scene_number: int
    dialogue: list[ScreenplayLineArtifact]


class ScreenplayActArtifact(BaseModel):
    act_number: int
    scenes: list[ScreenplaySceneArtifact]


class GenerationArtifactsResponse(BaseModel):
    run_id: uuid.UUID
    episode_plan: list[EpisodePlanActArtifact]
    beat_sheets: list[BeatSheetActArtifact]
    screenplay: list[ScreenplayActArtifact]


class V2ArtifactItem(BaseModel):
    id: str
    artifact_key: str
    version: int
    status: str
    provider: str | None = None
    model: str | None = None
    created_at: str | None = None
    payload: dict


class V2ArtifactsResponse(BaseModel):
    run_id: uuid.UUID
    artifacts: dict[str, list[V2ArtifactItem]]
    compare_v1_run_id: uuid.UUID | None = None
    compare_v2_run_id: uuid.UUID | None = None
    compare_v1_artifacts: GenerationArtifactsResponse | None = None
    compare_v2_artifacts: dict[str, list[V2ArtifactItem]] | None = None


class V2ToolTraceItem(BaseModel):
    id: str
    phase: str
    agent_name: str
    tool_name: str
    input_json: dict
    output_json: dict
    provider_call_id: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    created_at: str | None = None


class V2ToolTracesResponse(BaseModel):
    run_id: uuid.UUID
    traces: list[V2ToolTraceItem]
