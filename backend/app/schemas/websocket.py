from pydantic import BaseModel


class WSEvent(BaseModel):
    type: str
    phase: str | None = None
    step: str | None = None
    description: str | None = None
    detail: str | None = None
    duration_ms: int | None = None
    pct: float | None = None
    message: str | None = None
    count: int | None = None
    asset_types: list[str] | None = None
    claim_type: str | None = None
    claim_id: str | None = None
    statement_preview: str | None = None
    level: str | None = None
    brief_preview: str | None = None
    act_number: int | None = None
    act: int | None = None
    scene: int | None = None
    scenes: int | None = None
    reason: str | None = None
    source_title: str | None = None
    metrics: dict | None = None
    episode_id: str | None = None
    error: str | None = None
