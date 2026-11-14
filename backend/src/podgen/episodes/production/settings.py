"""Reuse exact existing profiles and instructions; never construct new model profiles."""

import json
from pathlib import Path
from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.journal import EvaluationJournal

from .contracts import ProductionSettings


def from_references(
    repo, project, state, research_template, audio_template, *, max_research_jobs, audio_budget
):
    state = json.loads(Path(state).read_text())
    research = json.loads(Path(research_template).read_text())
    audio = json.loads(Path(audio_template).read_text())
    manifest = EvaluationJournal(repo, project).manifest(UUID(state["run_id"]))
    if not manifest:
        raise ContractError("episode_reference_writing_manifest_missing")
    command = manifest["command"]
    shared = repo.get_runtime(project, RuntimeRef.model_validate(command["author_ref"]))
    acquisition = dict(shared["preparation"]["acquisition"], reuse_selection_ids=[])
    profiles = state["profiles"]
    return ProductionSettings(
        research_instructions=research["instructions"],
        research_profile=research["profile"],
        acquisition=acquisition,
        writer_profile_ref=profiles["author"],
        reviewer_profile_ref=profiles["editor"],
        planner_profile_ref=profiles["planner"],
        plan_reviewer_profile_ref=profiles["editor"],
        writing_limits=command["limits"],
        audio_profile=audio["profiles"][0],
        max_research_jobs=max_research_jobs,
        audio_max_cost_usd=audio_budget,
        accepts_variable_research_cost=True,
    )
