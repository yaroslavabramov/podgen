from __future__ import annotations

from app.generation_v2.source_triage_model_delivery import (
    question_workbench_delivery_state,
)
from app.generation_v2.source_triage_question_state import (
    finding_catalog_snapshot_id,
    question_input_snapshot_id,
    question_outcome_is_current,
)
from app.generation_v2.source_triage_set_state_views import (
    finding_catalog_page,
    question_workbench_page,
)


def _workspace() -> dict:
    return {
        "intent": {"language": "ru"},
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "question_ids": ["q-root", "q-child"],
            }
        ],
        "coverage_questions": [
            {
                "question_id": "q-root",
                "question": "Почему сохранилась система?",
                "parent_question_id": None,
                "answer_requirements": [
                    {
                        "requirement_id": "q-root:r1",
                        "description": "Установить причину сохранения.",
                        "completion_criterion": "Причинная связь установлена.",
                    }
                ],
            },
            {
                "question_id": "q-child",
                "question": "Как возникло первоначальное право?",
                "parent_question_id": "q-root",
                "answer_requirements": [
                    {
                        "requirement_id": "q-child:r1",
                        "description": "Установить первоначальное правило.",
                        "completion_criterion": "Первоначальное правило названо.",
                    }
                ],
            },
        ],
        "source_findings": [
            {
                "finding_id": "finding:S01:one",
                "set_id": "set-1",
                "source_ref": "S01",
                "statement": "Источник устанавливает только более позднюю практику.",
                "support_explanation": "Это прямо сказано в тексте.",
                "evidence_ids": ["evidence-1"],
                "passage_refs": ["P:S01:1"],
                "possible_question_ids": ["q-root", "q-child"],
                "scope": {
                    "actor": "Власти",
                    "period": "поздний период",
                    "geography": "",
                    "qualifiers": [],
                },
                "limitations": ["Не устанавливает первоначальное правило."],
                "question_links": [],
                "source_relations": [],
            }
        ],
        "coverage_outcomes": [],
        "source_read_receipts": [],
        "reading_notes": [],
    }


def _outcome(*, workspace: dict, question_id: str, requirement_id: str) -> dict:
    source_set = workspace["source_sets"][0]
    return {
        "set_id": "set-1",
        "question_id": question_id,
        "status": "partial",
        "finding_ids": ["finding:S01:one"],
        "requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "partial",
                "finding_ids": ["finding:S01:one"],
                "summary": "Установлена только поздняя практика.",
                "closure_basis": "Находка ограничена поздним периодом.",
                "remaining_uncertainty": "Первоначальное правило не установлено.",
                "missing_detail": "Первоначальное правило не установлено.",
            }
        ],
        "orientation_summary": "Установлена только поздняя практика.",
        "workbench_snapshot_id": question_input_snapshot_id(
            workspace,
            source_set,
            question_id=question_id,
        ),
    }


def test_child_outcome_invalidates_earlier_parent_outcome() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    parent = _outcome(
        workspace=workspace,
        question_id="q-root",
        requirement_id="q-root:r1",
    )
    workspace["coverage_outcomes"].append(parent)
    assert question_outcome_is_current(
        parent,
        workspace=workspace,
        source_set=source_set,
        question_id="q-root",
    )

    workspace["coverage_outcomes"].append(
        _outcome(
            workspace=workspace,
            question_id="q-child",
            requirement_id="q-child:r1",
        )
    )

    assert not question_outcome_is_current(
        parent,
        workspace=workspace,
        source_set=source_set,
        question_id="q-root",
    )
    page = question_workbench_page(
        workspace,
        source_set,
        question_id="q-root",
        cursor=0,
        limit=25,
    )
    assert page["dependent_question_ids"] == ["q-child"]
    assert page["unresolved_dependent_question_ids"] == []
    assert any(
        item.get("state_record_kind") == "dependent_question_result"
        and item.get("outcome_is_current") is True
        for item in page["items"]
    )


def test_finding_catalog_is_shared_and_workbench_does_not_repeat_evidence() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    catalog = finding_catalog_page(workspace, source_set, cursor=0, limit=25)
    workbench = question_workbench_page(
        workspace,
        source_set,
        question_id="q-child",
        cursor=0,
        limit=25,
    )

    assert catalog["finding_catalog_snapshot_id"] == finding_catalog_snapshot_id(
        workspace,
        source_set,
    )
    assert workbench["finding_catalog_snapshot_id"] == catalog["finding_catalog_snapshot_id"]
    assert any(item.get("finding_id") == "finding:S01:one" for item in catalog["items"])
    assert not any("snippet_preview" in item for item in workbench["items"])
    assert not any(item.get("statement") for item in workbench["items"])


def test_question_workbench_delivery_state_returns_first_gap() -> None:
    workspace = _workspace()
    workspace["source_triage_question_workbench_deliveries"] = [
        {
            "set_id": "set-1",
            "question_id": "q-root",
            "workbench_snapshot_id": "snapshot-1",
            "cursor": "0",
            "returned_count": 3,
            "total_count": 10,
            "delivery_status": "delivered",
        },
        {
            "set_id": "set-1",
            "question_id": "q-root",
            "workbench_snapshot_id": "snapshot-1",
            "cursor": "5",
            "returned_count": 2,
            "total_count": 10,
            "delivery_status": "delivered",
        },
    ]

    assert question_workbench_delivery_state(
        workspace,
        set_id="set-1",
        question_id="q-root",
        snapshot_id="snapshot-1",
    ) == {
        "complete": False,
        "total_count": 10,
        "delivered_count": 5,
        "first_unread_cursor": "3",
    }


def test_workbench_maps_requirement_id_to_owning_question() -> None:
    workspace = _workspace()
    result = question_workbench_page(
        workspace,
        workspace["source_sets"][0],
        question_id="q-child:r1",
        cursor=0,
        limit=25,
    )

    assert result["error"] == "question_workbench_requires_known_question_id"
    assert result["supplied_id_kind"] == "requirement_id"
    assert result["replace_with_question_ids"] == ["q-child"]
