from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.prompting.research import RESEARCH_JOB_PROMPT
from app.generation_v2.research_workspace import _jobs_from_corpus_brief
from app.generation_v2.research_workspace import _job_plan_for_new_attempt
from app.generation_v2.research_workspace_gap_helpers import dedupe_gaps
from app.generation_v2.research_workspace_gap_tools import record_research_gap_handler
from app.generation_v2.research_workspace_prompt_payloads import current_job_prompt_payload
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.schemas_source import CoverageOutcome, CoverageQuestion, ResearchHandoffItem


async def _gap_tool(monkeypatch: pytest.MonkeyPatch, workspace: dict):
    from app.generation_v2 import research_workspace as workspace_module

    async def load_workspace(_db, _run_id):
        return workspace

    async def save_workspace(_db, *, generation_run_id, workspace: dict, status=None):
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    monkeypatch.setattr(workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_research_workspace", save_workspace)
    return record_research_gap_handler(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        evidence_toolset=None,
        current_job_id=None,
    )


@pytest.mark.asyncio
async def test_blocking_gap_cannot_be_resolved_through_temporary_severity_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {
        "source_order": ["S01"],
        "atom_drafts": [],
        "research_gaps": [
            {
                "gap_id": "gap-central",
                "question": "Who independently corroborates the central claim?",
                "category": "weak_corroboration",
                "severity": "blocking",
                "treatment_relevance": "central",
                "source_refs": ["S01"],
                "evidence_ids": [],
                "atom_refs": [],
                "status": "open",
                "resolution_note": "",
            }
        ],
    }
    tool = await _gap_tool(monkeypatch, workspace)

    result = await tool(
        {
            "gap_id": "gap-central",
            "question": "Who independently corroborates the central claim?",
            "category": "weak_corroboration",
            "severity": "optional",
            "treatment_relevance": "minor",
            "source_refs": ["S01"],
            "evidence_ids": [],
            "atom_refs": [],
            "status": "resolved",
            "resolution_note": "Treat this as a minor caveat.",
        }
    )

    assert result == {
        "accepted": False,
        "error": "blocking research gaps cannot be resolved without evidence_ids or atom_refs",
    }
    assert workspace["research_gaps"][0]["status"] == "open"
    assert workspace["research_gaps"][0]["severity"] == "blocking"


@pytest.mark.asyncio
async def test_resolved_gap_can_be_reopened_or_deferred_by_later_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {
        "source_order": ["S01"],
        "atom_drafts": [],
        "research_gaps": [
            {
                "gap_id": "gap-central",
                "question": "Who independently corroborates the central claim?",
                "category": "weak_corroboration",
                "severity": "blocking",
                "treatment_relevance": "central",
                "source_refs": ["S01"],
                "evidence_ids": ["previous-evidence"],
                "atom_refs": [],
                "status": "resolved",
                "resolution_note": "Initially appeared resolved.",
            }
        ],
    }
    tool = await _gap_tool(monkeypatch, workspace)

    result = await tool(
        {
            "gap_id": "gap-central",
            "question": "Who independently corroborates the central claim?",
            "category": "weak_corroboration",
            "severity": "blocking",
            "treatment_relevance": "central",
            "source_refs": ["S01"],
            "evidence_ids": [],
            "atom_refs": [],
            "status": "deferred",
            "resolution_note": "A later read invalidated the apparent corroboration.",
        }
    )

    assert result["accepted"] is True
    assert workspace["research_gaps"][0]["status"] == "deferred"
    assert workspace["research_gaps"][0]["resolution_note"] == (
        "A later read invalidated the apparent corroboration."
    )


def test_legacy_merged_blocking_resolution_without_support_fails_open() -> None:
    gaps = dedupe_gaps(
        [
            {
                "gap_id": "gap-central",
                "question": "Who independently corroborates the central claim?",
                "category": "weak_corroboration",
                "severity": "blocking",
                "treatment_relevance": "central",
                "status": "open",
            },
            {
                "gap_id": "gap-central",
                "question": "Who independently corroborates the central claim?",
                "category": "weak_corroboration",
                "severity": "optional",
                "treatment_relevance": "minor",
                "status": "resolved",
                "resolution_note": "Treat this as a minor caveat.",
            },
        ]
    )

    assert len(gaps) == 1
    assert gaps[0]["severity"] == "blocking"
    assert gaps[0]["status"] == "open"


def test_seeded_job_preserves_structured_corpus_brief_outcome_lineage() -> None:
    source_id = str(uuid.uuid4())
    question = CoverageQuestion(
        question_id="q-mechanism",
        question="How does the mechanism work?",
        kind="mechanism",
        importance="critical",
    )
    outcome = CoverageOutcome(
        outcome_id="outcome:q-mechanism",
        question_id=question.question_id,
        status="partial",
        source_ids=[source_id],
        evidence_ids=["evidence-seed"],
        orientation_summary="The corpus establishes the first two steps.",
        missing_detail="The final dependency is not yet explained.",
        next_action="research",
    )
    handoff = ResearchHandoffItem(
        handoff_id="handoff:q-mechanism",
        objective=question.question,
        question_ids=[question.question_id],
        preferred_source_ids=[source_id],
        starting_evidence_ids=["evidence-seed"],
        action="corroborate",
        reason=outcome.missing_detail,
    )
    brief = SimpleNamespace(
        coverage_questions=[question],
        coverage_outcomes=[outcome],
        research_handoff=[handoff],
    )
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id=source_id,
                title="Mechanism source",
                source_type="report",
                summary="A detailed mechanism report.",
                coverage_status="ready",
            )
        ]
    )

    job = _jobs_from_corpus_brief(brief, inventory, allowed_source_refs=["S01"])[0]  # type: ignore[arg-type]
    payload = current_job_prompt_payload(job)

    assert job["coverage_question_ids"] == ["q-mechanism"]
    assert job["corpus_brief_outcomes"][0]["orientation_summary"] == (
        "The corpus establishes the first two steps."
    )
    assert job["corpus_brief_outcomes"][0]["missing_detail"] == (
        "The final dependency is not yet explained."
    )
    assert payload["corpus_handoff_id"] == "handoff:q-mechanism"
    assert payload["coverage_question_ids"] == ["q-mechanism"]
    assert payload["corpus_brief_lineage"]["outcomes"][0]["status"] == "partial"
    assert payload["corpus_brief_lineage"]["requires_full_outcome_read"] is True
    assert "corpus_brief_outcomes" in payload["corpus_brief_lineage"]["paging_instruction"]
    assert "corpus_brief_lineage.paging_instruction" in RESEARCH_JOB_PROMPT


def test_old_corpus_brief_lineage_is_not_carried_into_a_new_attempt() -> None:
    resumed = _job_plan_for_new_attempt(
        {
            "job_id": "old-handoff",
            "source_refs": ["S01"],
            "coverage_question_ids": ["q-old"],
            "corpus_brief_outcomes": [
                {
                    "question_id": "q-old",
                    "status": "partial",
                    "evidence_ids": ["old-attempt-evidence"],
                }
            ],
        },
        source_order=["S01"],
    )

    assert resumed["corpus_brief_outcomes"] == []
