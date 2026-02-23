from typing import TypedDict


class PipelineState(TypedDict):
    generation_run_id: str
    project_id: str
    user_id: str
    user_prompt: str
    duration_target: int  # seconds
    language: str  # "en" | "ru"
    show_bible_id: str
    source_ids: list[str]
    content_map_id: str
    manifest_id: str
    evidence_ids: list[str]
    pending_deep_ingestion_job_ids: list[str]
    claim_ids: list[str]
    conflict_brief_ids: list[str]
    script_segment_ids: list[str]
    episode_id: str
    current_phase: str
    iteration_counts: dict
    quality_signals: dict
