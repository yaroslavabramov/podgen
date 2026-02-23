from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class PipelineEvent:
    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict:
        return {"type": self.type, **self.data}


def phase_started(phase: str, description: str) -> PipelineEvent:
    return PipelineEvent("phase_started", {"phase": phase, "description": description})


def phase_completed(phase: str, duration_ms: int) -> PipelineEvent:
    return PipelineEvent("phase_completed", {"phase": phase, "duration_ms": duration_ms})


def step_started(phase: str, step: str, detail: str | None = None) -> PipelineEvent:
    return PipelineEvent("step_started", {"phase": phase, "step": step, "detail": detail})


def step_completed(phase: str, step: str) -> PipelineEvent:
    return PipelineEvent("step_completed", {"phase": phase, "step": step})


def progress(pct: float, message: str) -> PipelineEvent:
    return PipelineEvent("progress", {"pct": pct, "message": message})


def evidence_found(count: int, asset_types: list[str]) -> PipelineEvent:
    return PipelineEvent("evidence_found", {"count": count, "asset_types": asset_types})


def claim_created(claim_type: str, statement_preview: str) -> PipelineEvent:
    return PipelineEvent("claim_created", {"claim_type": claim_type, "statement_preview": statement_preview[:100]})


def claim_verified(claim_id: str, level: str) -> PipelineEvent:
    return PipelineEvent("claim_verified", {"claim_id": claim_id, "level": level})


def conflict_detected(brief_preview: str) -> PipelineEvent:
    return PipelineEvent("conflict_detected", {"brief_preview": brief_preview[:200]})


def act_planned(act_number: int, scenes: int) -> PipelineEvent:
    return PipelineEvent("act_planned", {"act_number": act_number, "scenes": scenes})


def scene_written(act: int, scene: int) -> PipelineEvent:
    return PipelineEvent("scene_written", {"act": act, "scene": scene})


def writer_revolt(act: int, scene: int, reason: str) -> PipelineEvent:
    return PipelineEvent("writer_revolt", {"act": act, "scene": scene, "reason": reason})


def audio_segment_generated(act: int, scene: int) -> PipelineEvent:
    return PipelineEvent("audio_segment_generated", {"act": act, "scene": scene})


def deep_ingestion_completed(source_title: str) -> PipelineEvent:
    return PipelineEvent("deep_ingestion_completed", {"source_title": source_title})


def quality_update(metrics: dict) -> PipelineEvent:
    return PipelineEvent("quality_update", {"metrics": metrics})


def generation_completed(episode_id: str) -> PipelineEvent:
    return PipelineEvent("generation_completed", {"episode_id": episode_id})


def generation_failed(error: str) -> PipelineEvent:
    return PipelineEvent("generation_failed", {"error": error})
