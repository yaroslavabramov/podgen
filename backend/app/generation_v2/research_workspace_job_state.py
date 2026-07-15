from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.generation_v2.research_workspace_helpers import (
    active_atom_drafts as _active_atom_drafts,
    dedupe as _dedupe,
)
from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


def workspace_job(workspace: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    for item in workspace.get("investigation_jobs") or []:
        if item.get("job_id") == job_id:
            return dict(item)
    return None


def replace_workspace_job(workspace: dict[str, Any], job: dict[str, Any]) -> None:
    job_id = job.get("job_id")
    workspace["investigation_jobs"] = [
        job if item.get("job_id") == job_id else item for item in workspace.get("investigation_jobs") or []
    ]


def refs_outside_job(job: dict[str, Any], source_refs: list[str]) -> list[str]:
    if bool(job.get("allow_broad_search")):
        return []
    allowed = set(str(item) for item in job.get("source_refs") or [])
    return [ref for ref in source_refs if ref not in allowed]


def invalid_workspace_refs(workspace: dict[str, Any], source_refs: list[str]) -> list[str]:
    source_order = set(str(item) for item in workspace.get("source_order") or [])
    return [ref for ref in source_refs if ref not in source_order]


def resolve_workspace_atom_refs(workspace: dict[str, Any], atom_refs: list[Any]) -> tuple[list[str], list[str]]:
    active_drafts = _active_atom_drafts(workspace.get("atom_drafts") or [])
    atom_id_by_ref: dict[str, str] = {}
    for idx, draft in enumerate(active_drafts, start=1):
        atom_id = str(draft.get("atom_id") or "").strip()
        if not atom_id:
            continue
        atom_id_by_ref[atom_id] = atom_id
        atom_id_by_ref[f"A{idx:02d}"] = atom_id
    resolved: list[str] = []
    invalid: list[str] = []
    for item in atom_refs:
        ref = str(item or "").strip()
        if not ref:
            continue
        atom_id = atom_id_by_ref.get(ref)
        if atom_id:
            if atom_id not in resolved:
                resolved.append(atom_id)
        else:
            invalid.append(ref)
    return resolved, invalid


async def invalid_job_evidence_ids(
    evidence_toolset: Any | None,
    job: dict[str, Any],
    evidence_ids: list[str],
    *,
    source_refs: list[str] | None = None,
) -> list[str]:
    if not evidence_ids or evidence_toolset is None:
        return []
    allowed_refs = set(str(item) for item in (source_refs or job.get("source_refs") or []))
    invalid: list[str] = []
    starting_evidence_ids = set(str(item) for item in job.get("starting_evidence_ids") or [])
    read_evidence_ids = set(getattr(evidence_toolset, "_research_read_evidence_ids", set()) or set())
    for evidence_id in evidence_ids:
        if evidence_id in starting_evidence_ids and evidence_id not in read_evidence_ids:
            invalid.append(evidence_id)
            continue
        evidence = await evidence_toolset._evidence_in_run(evidence_id)
        if evidence is None:
            invalid.append(evidence_id)
            continue
        evidence_ref = await evidence_toolset._source_ref_for_id(evidence.source_id)
        if not bool(job.get("allow_broad_search")) and evidence_ref not in set(job.get("source_refs") or []):
            invalid.append(evidence_id)
            continue
        if allowed_refs and evidence_ref not in allowed_refs:
            invalid.append(evidence_id)
    return invalid


async def invalid_workspace_evidence_ids(evidence_toolset: Any | None, evidence_ids: list[str]) -> list[str]:
    if not evidence_ids or evidence_toolset is None:
        return []
    invalid = []
    for evidence_id in evidence_ids:
        if await evidence_toolset._evidence_in_run(evidence_id) is None:
            invalid.append(evidence_id)
    return invalid


async def source_ids_for_refs(evidence_toolset: Any | None, inventory: SourceMap, refs: list[str]) -> list[str]:
    if evidence_toolset is not None:
        source_ids, invalid = await evidence_toolset._source_ids_from_refs_or_ids(refs)
        if not invalid:
            return source_ids
    context = build_source_ref_context(inventory)
    by_ref = {source_ref_for_id(card.source_id, context): card.source_id for card in inventory.source_cards}
    return _dedupe([by_ref.get(ref, "") for ref in refs])


def mark_research_progress(workspace: dict[str, Any], *, source_refs: list[str], evidence_ids: list[str]) -> None:
    workspace["covered_source_refs"] = _dedupe([*(workspace.get("covered_source_refs") or []), *source_refs])
    workspace["selected_evidence_ids"] = _dedupe([*(workspace.get("selected_evidence_ids") or []), *evidence_ids])


def reset_research_read_advisory(workspace: dict[str, Any]) -> None:
    workspace["reads_since_commit"] = 0
    workspace["commit_recommended"] = False


def source_order(inventory: SourceMap) -> list[str]:
    context = build_source_ref_context(inventory)
    return [source_ref_for_id(card.source_id, context) for card in inventory.source_cards]


def prepare_workspace_jobs_for_resume(workspace: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    current = dict(workspace or {})
    resumed_job_ids: list[str] = []
    jobs: list[dict[str, Any]] = []
    for item in current.get("investigation_jobs") or []:
        job = dict(item)
        status = str(job.get("status") or "planned")
        questions = [str(question).strip() for question in job.get("questions") or [] if str(question).strip()]
        outcomes = [outcome for outcome in job.get("question_outcomes") or [] if isinstance(outcome, dict)]
        complete_outcomes = _outcomes_cover_questions(questions, outcomes)
        if status == "in_progress" or (status in {"mapped", "deferred"} and not complete_outcomes):
            job["status"] = "planned"
            job["resume_reason"] = (
                "Previous research attempt stopped before durable completion."
                if status == "in_progress"
                else "Previous completion did not resolve or defer every planned question."
            )
            job["updated_at"] = datetime.now(UTC).isoformat()
            resumed_job_ids.append(str(job.get("job_id") or ""))
        jobs.append(job)
    if resumed_job_ids:
        resumed_set = set(resumed_job_ids)
        current["investigation_jobs"] = jobs
        current["processed_jobs"] = [
            item for item in current.get("processed_jobs") or [] if str(item.get("job_id") or "") not in resumed_set
        ]
        current["current_job"] = None
        current["memo_fields"] = {}
        current["status"] = "planned"
    return current, [job_id for job_id in resumed_job_ids if job_id]


def _outcomes_cover_questions(questions: list[str], outcomes: list[dict[str, Any]]) -> bool:
    def key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    expected = {key(question) for question in questions}
    actual = [key(outcome.get("question")) for outcome in outcomes]
    return (
        bool(expected)
        and set(actual) == expected
        and len(actual) == len(set(actual))
        and all(
            str(outcome.get("status") or "") in {"answered", "deferred"} and str(outcome.get("note") or "").strip()
            for outcome in outcomes
        )
    )


__all__ = [
    "invalid_job_evidence_ids",
    "invalid_workspace_evidence_ids",
    "invalid_workspace_refs",
    "mark_research_progress",
    "refs_outside_job",
    "replace_workspace_job",
    "reset_research_read_advisory",
    "resolve_workspace_atom_refs",
    "source_ids_for_refs",
    "source_order",
    "prepare_workspace_jobs_for_resume",
    "workspace_job",
]
