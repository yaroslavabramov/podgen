from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2.runtime_contracts import terminal_result_from_tool_output
from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash
from app.generation_v2.source_triage_lineage import (
    coverage_finalization_payload,
    coverage_terminal_completion_payload,
)
from app.generation_v2.tools.evidence_tool_write_specs import evidence_write_tools
from app.generation_v2.tools.evidence_write_actions import EvidenceWriteActionMixin


def _workspace() -> dict:
    question_texts = {
        "q_covered": "What is already established?",
        "q_partial": "P" * 279,
        "q_missing": "What material is entirely absent?",
        "q_conflicted": "Which accounts conflict, and what remains unresolved?",
        "q_deferred": "What cannot be checked with the current representation?",
    }
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "triage_semantic_revision": 7,
        "coverage_questions": [
            {"question_id": question_id, "question": question}
            for question_id, question in question_texts.items()
        ],
        "coverage_syntheses": [
            {
                "question_id": "q_covered",
                "status": "covered",
                "orientation_summary": "Established.",
                "missing_detail": "",
                "conflict_summary": "",
                "deferred_reason": "",
                "next_action": "research",
                "input_revision": 7,
            },
            {
                "question_id": "q_partial",
                "status": "partial",
                "orientation_summary": "Partly established.",
                "missing_detail": "M" * 1_600,
                "conflict_summary": "",
                "deferred_reason": "",
                "next_action": "source_acquisition",
                "input_revision": 7,
            },
            {
                "question_id": "q_missing",
                "status": "missing",
                "orientation_summary": "",
                "missing_detail": "Nothing in the current corpus answers this question.",
                "conflict_summary": "",
                "deferred_reason": "",
                "next_action": "research",
                "input_revision": 7,
            },
            {
                "question_id": "q_conflicted",
                "status": "conflicted",
                "orientation_summary": "Two incompatible accounts remain.",
                "missing_detail": "The decisive primary record is still absent.",
                "conflict_summary": "C" * 1_600,
                "deferred_reason": "",
                "next_action": "research",
                "input_revision": 7,
            },
            {
                "question_id": "q_deferred",
                "status": "deferred",
                "orientation_summary": "",
                "missing_detail": "",
                "conflict_summary": "",
                "deferred_reason": "D" * 1_000,
                "next_action": "source_acquisition",
                "input_revision": 7,
            },
        ],
        "coverage_outcomes": [],
        "source_sets": [],
        "source_assessments": [],
        "source_order": [],
    }
    for synthesis in workspace["coverage_syntheses"]:
        synthesis["input_hash"] = coverage_synthesis_input_hash(
            workspace,
            synthesis["question_id"],
        )
    return workspace


def _gap_notes(payload: dict) -> list[dict]:
    return [json.loads(note.removeprefix("coverage_gap:")) for note in payload["notes"]]


def test_backend_gap_ledger_preserves_every_full_gap_even_when_model_summary_omits_them() -> None:
    workspace = _workspace()

    finalization = coverage_finalization_payload(
        workspace,
        model_summary="Everything important is covered.",
    )
    terminal = coverage_terminal_completion_payload(finalization)

    assert finalization["model_summary"] == "Everything important is covered."
    assert finalization["coverage_question_count"] == 5
    assert finalization["coverage_status_counts"] == {
        "covered": 1,
        "partial": 1,
        "missing": 1,
        "conflicted": 1,
        "deferred": 1,
    }
    assert finalization["coverage_gap_count"] == 4
    assert [item["question_id"] for item in finalization["coverage_gaps"]] == [
        "q_partial",
        "q_missing",
        "q_conflicted",
        "q_deferred",
    ]
    notes = _gap_notes(terminal)
    assert notes == finalization["coverage_gaps"]
    assert notes[0]["question_text"] == "P" * 279
    assert notes[0]["missing_detail"] == "M" * 1_600
    assert notes[2]["missing_detail"] == "The decisive primary record is still absent."
    assert notes[2]["conflict_detail"] == "C" * 1_600
    assert notes[3]["deferred_detail"] == "D" * 1_000
    assert all(item["status"] != "covered" for item in notes)
    assert "coverage_gaps=4" in terminal["summary"]
    assert "Everything important is covered" not in terminal["summary"]


class _Owner(EvidenceWriteActionMixin):
    def __init__(self) -> None:
        self.db = None
        self.run = SimpleNamespace(id=uuid.uuid4())

    async def _selected_evidence_ids(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_terminal_completion_and_resume_use_the_same_backend_gap_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace()
    saved: list[dict] = []

    async def load_workspace(_db, _run_id):
        return workspace

    async def save_workspace(_db, *, workspace, **_kwargs):
        saved.append(deepcopy(workspace))
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.save_source_triage_workspace",
        save_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_triage_canonical.canonical_workspace_violations",
        lambda _workspace, require_syntheses: [],
    )
    owner = _Owner()
    tool = next(item for item in evidence_write_tools(owner) if item.name == "finalize_source_map")
    arguments = {"ready": True, "summary": "The model mentioned only one gap."}

    output = await tool.handler(arguments)
    direct = terminal_result_from_tool_output(tool, arguments=arguments, output=output)
    resumed = await tool.terminal_resume_loader()  # type: ignore[misc]

    assert output["accepted"] is True
    assert output["coverage_question_count"] == 5
    assert output["coverage_gap_count"] == 4
    assert direct is not None and direct.accepted is True
    assert resumed is not None and resumed.accepted is True
    assert direct.completion == resumed.completion
    assert len(direct.completion.notes) == 4
    assert _gap_notes(direct.completion.model_dump()) == saved[-1]["source_map_finalization"]["coverage_gaps"]
    assert saved[-1]["source_map_finalization"]["model_summary"] == arguments["summary"]


def test_old_finalized_workspace_remains_readable_without_invented_counts() -> None:
    assert coverage_terminal_completion_payload(
        {"ready": True, "summary": "Legacy finalization."}
    ) == {
        "ready": True,
        "summary": "Legacy finalization.",
        "notes": [],
    }
