from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.generation_v2.source_triage_set_state_views import (
    question_workbench_signature,
)
from app.generation_v2.agents.tool_sets import TRIAGE_SEMANTIC_EVAL_TOOL_NAMES
from app.generation_v2.source_triage_workspace_prompting import source_set_prompt_payload
from app.generation_v2.runtime_tool_calls import _validated_private_runtime_directive
from app.generation_v2.triage_semantic_eval_runner import semantic_eval_report
from app.generation_v2.triage_semantic_eval_state import (
    build_semantic_eval_workspace,
    semantic_eval_evidence_ids,
)


def _source_workspace(long_text: str = "") -> dict:
    return {
        "schema_version": "source_triage_workspace.v3",
        "_workspace_revision": 11,
        "source_order": ["S01"],
        "intent": {"language": "ru"},
        "central_question_id": "root",
        "coverage_questions": [
            {
                "question_id": "root",
                "question": "Почему сохранилась система?",
                "answer_requirements": [
                    {"requirement_id": "root:history", "completion_criterion": "Есть хронология."},
                    {"requirement_id": "root:cause", "completion_criterion": "Есть причина."},
                ],
            },
            {
                "question_id": "origin",
                "parent_question_id": "root",
                "question": "Как возникло право?",
                "answer_requirements": [
                    {"requirement_id": "origin:mechanism", "completion_criterion": "Есть механизм."}
                ],
            },
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "in_progress",
                "source_refs": ["S01"],
                "question_ids": ["root", "origin"],
                "planned_question_ids": ["root", "origin"],
                "question_assignments": [],
                "evidence_ids": ["old-evidence"],
                "pending_exact_reads": [{"source_ref": "S01"}],
            }
        ],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "processing_status": "resolved",
                "finding_ids": ["finding-1"],
                "evidence_ids": ["old-evidence"],
                "source_summary": long_text,
                "reading_basis": {"read_state_signature": "stable"},
            }
        ],
        "source_findings": [
            {
                "set_id": "set-1",
                "finding_id": "finding-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "statement": long_text or "Установлен ограниченный исторический факт.",
                "evidence_ids": ["old-evidence"],
                "evidence_previews": [{"evidence_id": "old-evidence", "snippet_preview": "quote"}],
                "question_links": [
                    {"question_id": "root", "requirement_ids": ["root:cause"]}
                ],
                "possible_question_ids": ["root", "origin"],
                "source_relations": [{"kind": "supports", "evidence_ids": ["old-evidence"]}],
            }
        ],
        "source_read_receipts": [
            {"set_id": "set-1", "source_ref": "S01", "receipt_ref": "receipt-1"}
        ],
        "reading_notes": [{"set_id": "set-1", "source_ref": "S01", "note_id": "note-1"}],
        "source_structure_pages": [],
        "coverage_outcomes": [{"set_id": "set-1", "question_id": "root", "status": "covered"}],
        "coverage_syntheses": [{"set_id": "set-1", "question_id": "root"}],
    }


def test_semantic_eval_state_keeps_full_reading_memory_but_removes_old_decisions() -> None:
    long_text = "Д" * 20_000
    source = _source_workspace(long_text)
    fresh = {
        "schema_version": "source_triage_workspace.v3",
        "_workspace_revision": 3,
        "source_order": ["S01"],
        "intent": {"language": "ru"},
    }

    workspace = build_semantic_eval_workspace(
        fresh_workspace=fresh,
        source_workspace=source,
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )

    source_set = workspace["source_sets"][0]
    assert source_set["status"] == "planned"
    assert workspace["triage_semantic_eval"]["mode"] == "frozen_post_reading_state"
    assert "pending_exact_reads" not in source_set
    assert source_set["question_assignments"] == [
        {"question_id": "root", "requirement_ids": ["root:history", "root:cause"]},
        {"question_id": "origin", "requirement_ids": ["origin:mechanism"]},
    ]
    assert workspace["coverage_outcomes"] == []
    assert workspace["coverage_syntheses"] == []
    assert workspace["source_findings"][0]["question_links"] == []
    assert workspace["source_findings"][0]["source_relations"][0]["evidence_ids"] == [
        "new-evidence"
    ]
    assert workspace["source_findings"][0]["evidence_previews"][0]["evidence_id"] == (
        "new-evidence"
    )
    assert semantic_eval_evidence_ids(workspace) == ["new-evidence"]
    assert workspace["source_assessments"][0]["source_summary"] == long_text
    assert workspace["source_findings"][0]["statement"] == long_text


def test_semantic_eval_prompt_payload_declares_frozen_decision_mode() -> None:
    workspace = build_semantic_eval_workspace(
        fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
        source_workspace=_source_workspace(),
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )

    payload = source_set_prompt_payload(workspace, "set-1")

    assert payload["set"]["execution_mode"] == "semantic_eval"
    assert "frozen" in payload["set"]["execution_goal"]
    assert "do not read sources" in payload["set"]["execution_goal"]


def test_semantic_eval_tool_contract_is_semantic_only() -> None:
    assert TRIAGE_SEMANTIC_EVAL_TOOL_NAMES == {
        "read_triage_set_state",
        "read_evidence",
        "resolve_triage_question",
        "finish_triage_source_set",
    }


def test_semantic_eval_state_fails_closed_when_evidence_clone_is_incomplete() -> None:
    with pytest.raises(ValueError, match="evidence clone is incomplete"):
        build_semantic_eval_workspace(
            fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
            source_workspace=_source_workspace(),
            set_id="set-1",
            question_ids=["root", "origin"],
            evidence_id_map={},
        )


def test_finding_catalog_page_can_schedule_same_chain_delivery() -> None:
    messages, receipt_refs, error = _validated_private_runtime_directive(
        tool_name="read_triage_set_state",
        output={
            "accepted": True,
            "section": "finding_catalog",
            "finding_catalog_receipt_ref": "finding_catalog_receipt:1234567890abcdef",
        },
        raw_runtime_messages=[],
        raw_receipt_delivery={
            "receipt_refs": ["finding_catalog_receipt:1234567890abcdef"]
        },
        has_runtime_messages=False,
        has_receipt_delivery=True,
    )

    assert error is None
    assert messages == []
    assert receipt_refs == ["finding_catalog_receipt:1234567890abcdef"]


def test_semantic_eval_report_requires_current_partial_causal_answers_without_rereading() -> None:
    workspace = build_semantic_eval_workspace(
        fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
        source_workspace=_source_workspace(),
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )
    source_set = workspace["source_sets"][0]
    source_set["status"] = "mapped"
    origin = _outcome("origin", "origin:mechanism")
    origin["workbench_snapshot_id"] = question_workbench_signature(
        workspace,
        source_set,
        question_id="origin",
    )
    workspace["coverage_outcomes"].append(origin)
    root = _outcome("root", "root:cause")
    root["requirement_results"].insert(
        0,
        {
            "requirement_id": "root:history",
            "status": "covered",
            "finding_ids": ["finding-1"],
            "summary": "Хронология установлена по сохранённым находкам.",
            "closure_basis": "Находка прямо фиксирует исторический этап.",
            "missing_detail": "",
        },
    )
    root["workbench_snapshot_id"] = question_workbench_signature(
        workspace,
        source_set,
        question_id="root",
    )
    workspace["coverage_outcomes"].append(root)
    traces = [
        SimpleNamespace(
            tool_name="finish_triage_source_set",
            output_json={"accepted": True},
        )
    ]

    report = semantic_eval_report(
        workspace=workspace,
        source_set=source_set,
        traces=traces,
        question_ids=["root", "origin"],
        expected_question_statuses={"root": "partial", "origin": "partial"},
        expected_requirement_statuses={
            ("root", "root:cause"): "partial",
            ("origin", "origin:mechanism"): "partial",
        },
    )

    assert report["passed"] is True
    assert report["source_read_call_count"] == 0
    assert report["accepted_finish_count"] == 1


def _outcome(question_id: str, requirement_id: str) -> dict:
    return {
        "outcome_id": f"outcome:{question_id}:set-1",
        "set_id": "set-1",
        "question_id": question_id,
        "status": "partial",
        "finding_ids": ["finding-1"],
        "evidence_ids": ["new-evidence"],
        "requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "partial",
                "finding_ids": ["finding-1"],
                "summary": "Сохранённые находки устанавливают только более поздний контекст.",
                "closure_basis": "Находка подтверждает контекст, но не исходный причинный механизм.",
                "remaining_uncertainty": "Не установлен первоначальный механизм допуска.",
                "missing_detail": "Не установлен первоначальный механизм допуска.",
            }
        ],
        "orientation_summary": "Установлен контекст, но причинная связь остаётся открытой.",
        "missing_detail": "Не установлен первоначальный механизм допуска.",
        "is_stale": False,
    }
