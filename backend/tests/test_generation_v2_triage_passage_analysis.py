from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2.source_triage_passage_analysis import (
    normalize_passage_analysis_args,
    preserve_downstream_fields_for_unchanged_findings,
)
from app.generation_v2.source_triage_exact_duplicates import exact_duplicate_predecessor
from app.generation_v2.tools.evidence_refs import SourceRefContext


def _finding() -> dict:
    return {
        "finding_id": "finding:S01:one",
        "set_id": "set-1",
        "source_ref": "S01",
        "source_id": "source-1",
        "source_version_ids": ["version-1"],
        "assessment_id": "assessment:source-1",
        "statement": "Один ограниченный факт.",
        "support_explanation": "Фрагмент прямо подтверждает факт.",
        "evidence_ids": ["evidence-1"],
        "passage_refs": ["passage-1"],
        "scope": {"actor": "A", "period": "P", "geography": "", "qualifiers": []},
        "limitations": [],
        "possible_question_ids": ["q1"],
        "question_links": [],
        "source_relations": [],
    }


def test_identical_source_analysis_preserves_question_links_and_relations() -> None:
    previous = _finding()
    previous["question_links"] = [
        {"question_id": "q1", "requirement_ids": ["q1:r1"], "rationale": "Прямо."}
    ]
    previous["source_relations"] = [
        {"related_source_ref": "S02", "relationship": "updates", "evidence_ids": ["evidence-1"]}
    ]
    current = _finding()

    preserve_downstream_fields_for_unchanged_findings(
        [current],
        previous_findings=[previous],
    )

    assert current["question_links"] == previous["question_links"]
    assert current["source_relations"] == previous["source_relations"]


def test_changed_source_owned_finding_invalidates_downstream_links() -> None:
    previous = _finding()
    previous["question_links"] = [
        {"question_id": "q1", "requirement_ids": ["q1:r1"], "rationale": "Прямо."}
    ]
    current = deepcopy(_finding())
    current["statement"] = "Изменённый факт."

    preserve_downstream_fields_for_unchanged_findings(
        [current],
        previous_findings=[previous],
    )

    assert current["question_links"] == []


@pytest.mark.asyncio
async def test_exact_duplicate_predecessor_uses_persisted_content_identity() -> None:
    first_source_id = uuid.uuid4()
    second_source_id = uuid.uuid4()
    first_version_id = uuid.uuid4()
    second_version_id = uuid.uuid4()
    cards = {
        str(first_source_id): SimpleNamespace(source_version_ids=[first_version_id]),
        str(second_source_id): SimpleNamespace(source_version_ids=[second_version_id]),
    }
    context = SourceRefContext(
        inventory=SimpleNamespace(source_cards=list(cards.values())),
        source_id_by_ref={"S01": str(first_source_id), "S02": str(second_source_id)},
        ref_by_source_id={str(first_source_id): "S01", str(second_source_id): "S02"},
        card_by_source_id=cards,
    )

    class Result:
        def all(self):
            return [
                (first_source_id, "extracted_text", "same-hash"),
                (second_source_id, "extracted_text", "same-hash"),
            ]

    class DB:
        async def execute(self, _query):
            return Result()

    match = await exact_duplicate_predecessor(
        DB(),
        ref_context=context,
        ordered_source_refs=["S01", "S02"],
        source_ref="S02",
    )

    assert match == {
        "source_ref": "S01",
        "matched_representation_kinds": ["extracted_text"],
    }


@pytest.mark.asyncio
async def test_passage_analysis_preserves_duplicate_identity_and_verified_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exact_match(*_args, **_kwargs):
        return {"source_ref": "S01", "matched_representation_kinds": ["original_file"]}

    monkeypatch.setattr(
        "app.generation_v2.source_triage_passage_analysis.exact_duplicate_predecessor",
        exact_match,
    )
    owner = SimpleNamespace(
        db=object(),
        inventory=SimpleNamespace(source_cards=[]),
        evidence_toolset=None,
    )

    normalized, error = await normalize_passage_analysis_args(
        owner,
        workspace={"coverage_questions": []},
        source_set={"set_id": "set-1", "source_refs": ["S01", "S02"]},
        source_ref="S02",
        args={
            "representation_status": "readable",
            "uniqueness": "duplicate",
            "uniqueness_reason": "Система подтвердила идентичное содержимое.",
            "duplicate_of_source_ref": "S01",
            "findings": [],
        },
    )

    assert error is None
    assert normalized["uniqueness"] == "duplicate"
    assert normalized["duplicate_of_source_ref"] == "S01"
    assert normalized["verified_exact_duplicate_of_source_ref"] == "S01"


@pytest.mark.asyncio
async def test_requirement_id_in_possible_questions_returns_owning_question_correction() -> None:
    class EvidenceToolset:
        async def materialize_source_passages(self, **_kwargs):
            raise AssertionError("Question references must be validated before evidence side effects")

    workspace = {
        "intent": {"language": "ru"},
        "coverage_questions": [
            {
                "question_id": "q-mechanism",
                "answer_requirements": [{"requirement_id": "q-mechanism:r2"}],
            },
            {"question_id": "q-history", "answer_requirements": []},
        ],
    }
    args = {
        "representation_status": "readable",
        "findings": [
            {
                **_finding(),
                "passage_refs": ["P:S01:1"],
                "possible_question_ids": ["q-mechanism:r2"],
            }
        ],
    }

    _normalized, error = await normalize_passage_analysis_args(
        SimpleNamespace(evidence_toolset=EvidenceToolset()),
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        args=args,
    )

    assert error == {
        "accepted": False,
        "error": "possible_question_ids_must_use_question_ids_not_requirement_ids",
        "source_ref": "S01",
        "finding_index": 0,
        "invalid_possible_question_ids": ["q-mechanism:r2"],
        "question_id_corrections": [
            {
                "supplied_id": "q-mechanism:r2",
                "supplied_id_kind": "requirement_id",
                "replace_with_question_ids": ["q-mechanism"],
            }
        ],
        "allowed_question_ids": ["q-history", "q-mechanism"],
        "output_language": "ru",
        "retry_policy": {
            "preserve_all_findings": True,
            "preserve_all_other_fields": True,
            "replace_only_invalid_possible_question_ids": True,
        },
        "next_action": (
            "Retry the same complete source analysis. Replace only the invalid possible_question_ids using the "
            "returned owning question_id values; do not remove findings, evidence, or other question links."
        ),
    }


@pytest.mark.asyncio
async def test_all_passage_refs_are_validated_before_any_evidence_is_written() -> None:
    materialization_calls = 0

    class EvidenceToolset:
        async def materialize_source_passages(self, **_kwargs):
            nonlocal materialization_calls
            materialization_calls += 1
            return {"accepted": True, "evidence_ids": ["evidence-1"]}

    workspace = {
        "intent": {"language": "ru"},
        "coverage_questions": [{"question_id": "q1", "answer_requirements": []}],
        "source_read_receipts": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "delivery_status": "delivered",
                "passage_refs": ["canonical-passage-1"],
                "passage_handles": [
                    {
                        "passage_ref": "P:S01:1",
                        "canonical_passage_ref": "canonical-passage-1",
                    }
                ],
            }
        ],
    }
    first = {**_finding(), "passage_refs": ["P:S01:1"]}
    second = {
        **_finding(),
        "finding_id": "finding:S01:two",
        "passage_refs": ["P:S01:999"],
    }

    _normalized, error = await normalize_passage_analysis_args(
        SimpleNamespace(evidence_toolset=EvidenceToolset()),
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        args={
            "representation_status": "readable",
            "findings": [first, second],
        },
    )

    assert error is not None
    assert error["error"] == "passage_ref_not_delivered_to_current_set_agent"
    assert error["finding_index"] == 1
    assert error["passage_ref_corrections"][0]["replacement_candidates"] == [
        "P:S01:1"
    ]
    assert materialization_calls == 0


@pytest.mark.asyncio
async def test_search_passage_requires_exact_window_before_finding_persistence() -> None:
    class EvidenceToolset:
        async def materialize_source_passages(self, **_kwargs):
            raise AssertionError("Search navigation must not be materialized as finding evidence")

    workspace = {
        "coverage_questions": [{"question_id": "q1", "answer_requirements": []}],
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:search:1",
                "set_id": "set-1",
                "source_ref": "S01",
                "read_kind": "search",
                "delivery_status": "delivered",
                "window_ref": "window:one",
                "start_offset": 100,
                "end_offset": 500,
                "passage_refs": ["canonical-search-passage"],
                "passage_handles": [
                    {
                        "passage_ref": "P:S01:1",
                        "canonical_passage_ref": "canonical-search-passage",
                    }
                ],
            }
        ],
    }
    finding = {
        **_finding(),
        "passage_refs": ["P:S01:1"],
        "evidence_ids": [],
    }

    _normalized, error = await normalize_passage_analysis_args(
        SimpleNamespace(evidence_toolset=EvidenceToolset()),
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        args={"representation_status": "readable", "findings": [finding]},
    )

    assert error is not None
    assert error["error"] == "search_passage_requires_exact_read"
    assert error["navigation_only_passage_refs"] == ["P:S01:1"]
    assert error["exact_read_candidates"] == [
        {
            "receipt_ref": "receipt:search:1",
            "window_ref": "window:one",
            "range": {"start_offset": 100, "end_offset": 500},
        }
    ]


@pytest.mark.asyncio
async def test_exact_window_evidence_can_support_finding_without_passage_ref() -> None:
    evidence = SimpleNamespace(source_id="source-1")

    class EvidenceToolset:
        async def _evidence_in_run(self, evidence_id: str):
            return evidence if evidence_id == "evidence-exact" else None

        async def _source_ref_for_id(self, source_id: str) -> str:
            return "S01" if source_id == "source-1" else ""

        def _evidence_has_current_exact_read(self, item) -> bool:
            return item is evidence

        async def materialize_source_passages(self, **_kwargs):
            raise AssertionError("No passage materialization is needed")

    finding = {
        **_finding(),
        "passage_refs": [],
        "evidence_ids": ["evidence-exact"],
    }
    normalized, error = await normalize_passage_analysis_args(
        SimpleNamespace(evidence_toolset=EvidenceToolset()),
        workspace={
            "coverage_questions": [{"question_id": "q1", "answer_requirements": []}],
        },
        source_set={"set_id": "set-1"},
        source_ref="S01",
        args={"representation_status": "readable", "findings": [finding]},
    )

    assert error is None
    assert normalized["findings"][0]["evidence_ids"] == ["evidence-exact"]
