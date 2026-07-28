from __future__ import annotations

import json
from types import SimpleNamespace
import hashlib
import uuid

import pytest

from app.generation_v2.source_triage_model_delivery import (
    mark_runtime_delivery_receipts_delivered,
    question_workbench_is_fully_delivered,
    semantic_review_receipt_ref,
    source_structure_coverage,
    stage_semantic_review_page,
    stage_source_structure_page,
)
from app.generation_v2.source_triage_set_review import (
    record_semantic_review_page,
    semantic_review_page,
    semantic_self_check_page,
    set_semantic_review_is_current,
    set_semantic_review_signature,
    set_semantic_self_check_is_current,
)
from app.generation_v2.source_triage_set_state_views import (
    finding_catalog_page,
    question_memory_page,
    question_workbench_page,
    question_workbench_signature,
    set_overview_page,
    source_memory_page,
)
from app.generation_v2.tools.evidence_legacy_receipts import hydrate_legacy_source_receipts


def _workspace() -> dict:
    return {
        "intent": {"language": "ru"},
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "question_ids": ["q1", "q2"],
            }
        ],
        "coverage_questions": [
            {
                "question_id": "q1",
                "question": "What happened?",
                "answer_requirements": [
                    {"requirement_id": "q1:r1", "description": "Establish the event."}
                ],
            },
            {
                "question_id": "q2",
                "question": "Why did it happen?",
                "answer_requirements": [
                    {"requirement_id": "q2:r1", "description": "Establish the cause."}
                ],
            },
        ],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "processing_status": "resolved",
                "representation_status": "readable",
                "disposition": "anchor",
                "finding_ids": ["finding-1"],
            }
        ],
        "source_findings": [
            {
                "finding_id": "finding-1",
                "set_id": "set-1",
                "source_ref": "S01",
                "statement": "One bounded finding supports both questions.",
                "support_explanation": "The same passage establishes the event and its stated cause.",
                "evidence_ids": ["evidence-1"],
                "scope": {"actor": "Actor", "period": "Period", "geography": "", "qualifiers": []},
                "limitations": [],
                "question_links": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"], "rationale": "Event."},
                    {"question_id": "q2", "requirement_ids": ["q2:r1"], "rationale": "Cause."},
                ],
                "source_relations": [],
            }
        ],
        "coverage_outcomes": [
            _outcome("q1", "q1:r1"),
            _outcome("q2", "q2:r1"),
        ],
        "source_read_receipts": [],
        "reading_notes": [],
    }


def _outcome(question_id: str, requirement_id: str) -> dict:
    return {
        "set_id": "set-1",
        "question_id": question_id,
        "status": "covered",
        "finding_ids": ["finding-1"],
        "requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "covered",
                "finding_ids": ["finding-1"],
                "summary": "Covered by the bounded finding.",
            }
        ],
        "orientation_summary": "Covered by the bounded finding.",
    }


def _read_review(workspace: dict, *, evidence_text: str) -> list[dict]:
    source_set = workspace["source_sets"][0]
    pages = []
    cursor = 0
    while True:
        page = semantic_review_page(
            workspace,
            source_set,
            evidence_by_id={
                "evidence-1": {
                    "evidence_id": "evidence-1",
                    "snippet_text": evidence_text,
                    "locator": "section 1",
                }
            },
            cursor=cursor,
            limit=2,
        )
        assert page["accepted"] is True
        record_semantic_review_page(workspace, source_set, page)
        pages.append(page)
        next_cursor = str(page["page"]["next_cursor"] or "")
        if not next_cursor:
            return pages
        cursor = int(next_cursor)


def test_question_memory_groups_one_finding_under_each_linked_question_part() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]

    q1 = question_memory_page(workspace, source_set, question_id="q1", cursor=0, limit=25)
    q2 = question_memory_page(workspace, source_set, question_id="q2", cursor=0, limit=25)

    for page, requirement_id in ((q1, "q1:r1"), (q2, "q2:r1")):
        assert page["output_language"] == "ru"
        allowed = [
            item
            for item in page["items"]
            if item.get("part_kind") == "allowed_finding"
        ]
        assert allowed == [
            {
                "state_record_kind": "requirement_findings",
                "owner_ref": f"question:{page['question_id']}:requirement:{requirement_id}",
                "record_kind": "requirement_findings",
                "part_kind": "allowed_finding",
                "index": 0,
                "finding_id": "finding-1",
                "source_ref": "S01",
                "statement": "One bounded finding supports both questions.",
            }
        ]


def test_question_workbench_puts_candidate_finding_statements_next_to_question() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    page = question_workbench_page(workspace, source_set, question_id="q1", cursor=0, limit=100)

    candidates = [
        item
        for item in page["items"]
        if item.get("state_record_kind") == "question_candidate_finding"
    ]

    assert candidates
    assert any(
        item.get("finding_id") == "finding-1"
        and item.get("statement") == "One bounded finding supports both questions."
        and item.get("scope") == {
            "actor": "Actor",
            "period": "Period",
            "geography": "",
            "qualifiers": [],
        }
        for item in candidates
    )
def test_finding_catalog_exposes_every_set_finding_once_before_linking() -> None:
    workspace = _workspace()
    workspace["source_findings"][0]["question_links"] = []
    workspace["source_findings"][0]["evidence_previews"] = [
        {
            "evidence_id": "evidence-1",
            "snippet_preview": "Полный сохранённый фрагмент.",
            "snippet_chars": 29,
            "snippet_truncated": False,
            "locator": "section 1",
        }
    ]

    page = finding_catalog_page(
        workspace,
        workspace["source_sets"][0],
        cursor=0,
        limit=100,
    )

    assert page["accepted"] is True
    assert any(
        item.get("state_record_kind") == "finding_catalog_item"
        and item.get("owner_ref") == "finding-1"
        for item in page["items"]
    )
    assert not any(
        item.get("state_record_kind") == "candidate_evidence"
        for item in page["items"]
    )


def test_resolving_one_question_does_not_invalidate_another_question_workbench() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    q1_before = question_workbench_signature(workspace, source_set, question_id="q1")
    q2_before = question_workbench_signature(workspace, source_set, question_id="q2")

    workspace["source_findings"][0]["question_links"][0]["rationale"] = "Обновлено."
    workspace["coverage_outcomes"][0]["orientation_summary"] = "Обновлённый итог q1."

    assert question_workbench_signature(workspace, source_set, question_id="q1") == q1_before
    assert question_workbench_signature(workspace, source_set, question_id="q2") == q2_before


def test_new_set_finding_invalidates_every_question_input_snapshot() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    before = {
        question_id: question_workbench_signature(workspace, source_set, question_id=question_id)
        for question_id in ("q1", "q2")
    }
    for outcome in workspace["coverage_outcomes"]:
        outcome["workbench_snapshot_id"] = before[str(outcome["question_id"])]

    workspace["source_findings"].append(
        {
            **workspace["source_findings"][0],
            "finding_id": "finding-2",
            "statement": "A later finding may alter either question.",
            "question_links": [],
        }
    )

    overview = set_overview_page(workspace, source_set, cursor=0, limit=25)

    assert all(
        question_workbench_signature(workspace, source_set, question_id=question_id)
        != before[question_id]
        for question_id in ("q1", "q2")
    )
    assert overview["state"]["stale_outcome_question_ids"] == ["q1", "q2"]
    assert overview["state"]["missing_outcome_question_ids"] == ["q1", "q2"]


def test_large_finding_catalog_item_is_losslessly_paged() -> None:
    workspace = _workspace()
    finding = workspace["source_findings"][0]
    finding["statement"] = "У" * 2_400
    finding["support_explanation"] = "Д" * 1_600
    finding["limitations"] = [f"Ограничение {index}: " + "л" * 760 for index in range(12)]
    finding["passage_refs"] = [
        f"passage:00000000-0000-0000-0000-000000000001:{index * 100}:{index * 100 + 99}:abcdef0123456789"
        for index in range(160)
    ]
    finding["possible_question_ids"] = [f"q{index}" for index in range(200)]

    cursor = 0
    returned_parts: list[dict] = []
    while True:
        page = finding_catalog_page(
            workspace,
            workspace["source_sets"][0],
            cursor=cursor,
            limit=2,
        )
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False)) <= 12_000
        returned_parts.extend(page["items"])
        next_cursor = str(page["page"]["next_cursor"] or "")
        if not next_cursor:
            break
        cursor = int(next_cursor)

    finding_parts = [
        item for item in returned_parts if item.get("record_kind") == "candidate_source_finding"
    ]
    assert any(item.get("part_kind") == "core" for item in finding_parts)
    assert sum(
        len(item.get("items") or [])
        for item in finding_parts
        if item.get("part_kind") == "finding_limitations"
    ) == 12
    assert not any(item.get("part_kind") == "finding_passage_refs" for item in finding_parts)
    assert sum(
        len(item.get("items") or [])
        for item in finding_parts
        if item.get("part_kind") == "finding_possible_question_ids"
    ) == 200


def test_source_and_question_memory_page_multiple_full_evidence_previews() -> None:
    workspace = _workspace()
    finding = workspace["source_findings"][0]
    finding["passage_refs"] = [f"passage:source:0:{index}:digest" for index in range(4)]
    finding["evidence_ids"] = [f"evidence-{index}" for index in range(4)]
    finding["evidence_previews"] = [
        {
            "evidence_id": f"evidence-{index}",
            "snippet_preview": "Т" * 3_000,
            "snippet_chars": 3_000,
            "snippet_truncated": False,
            "locator": f"section {index}",
        }
        for index in range(4)
    ]

    for reader, kwargs in (
        (source_memory_page, {"source_ref": "S01"}),
        (question_memory_page, {"question_id": "q1"}),
    ):
        cursor = 0
        returned_previews = 0
        while True:
            page = reader(
                workspace,
                workspace["source_sets"][0],
                cursor=cursor,
                limit=2,
                **kwargs,
            )
            assert page["accepted"] is True
            assert len(json.dumps(page, ensure_ascii=False)) <= 12_000
            returned_previews += sum(
                len(item.get("items") or [])
                for item in page["items"]
                if item.get("part_kind") == "finding_evidence_previews"
            )
            next_cursor = str(page["page"]["next_cursor"] or "")
            if not next_cursor:
                break
            cursor = int(next_cursor)
        assert returned_previews == 4


@pytest.mark.asyncio
async def test_legacy_receipt_is_replayed_from_pinned_text_without_a_new_read() -> None:
    text = "Первый абзац.\n\nВторой абзац с точным доказательством."
    version = SimpleNamespace(id=uuid.uuid4())

    class Owner:
        async def _resolve_source_version(self, **_kwargs):
            return version

        def _cached_source_version_text(self, _version):
            return text

        async def _source_allows_timestamp_locators(self, _version):
            return False

    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:old",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": str(version.id),
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "start_offset": 0,
                "end_offset": len(text),
            }
        ]
    }

    result = await hydrate_legacy_source_receipts(
        Owner(),
        workspace,
        set_id="set-1",
        source_ref="S01",
    )

    receipt = workspace["source_read_receipts"][0]
    assert result["accepted"] is True
    assert result["changed"] is True
    assert receipt["passage_refs"]
    assert [item["passage_ref"] for item in receipt["passage_handles"]] == [
        item["passage_ref"] for item in receipt["replay_passages"]
    ]
    assert all(
        item["passage_ref"].startswith("P:S01:")
        for item in receipt["replay_passages"]
    )
    assert "".join(item["text"] for item in receipt["replay_passages"]) == text
def test_semantic_review_includes_parent_child_comparison_component() -> None:
    workspace = _workspace()
    workspace["source_sets"][0]["question_ids"] = ["q2"]
    workspace["coverage_questions"][1]["parent_question_id"] = "q1"
    workspace["source_findings"][0]["question_links"] = [
        {"question_id": "q2", "requirement_ids": ["q2:r1"], "rationale": "Cause."}
    ]
    workspace["coverage_outcomes"] = [_outcome("q2", "q2:r1")]

    page = semantic_review_page(
        workspace,
        workspace["source_sets"][0],
        evidence_by_id={
            "evidence-1": {
                "evidence_id": "evidence-1",
                "snippet_text": "Evidence.",
                "locator": "section 1",
            }
        },
        cursor=0,
        limit=100,
    )

    question_ids = {
        str(item.get("question_id") or "")
        for item in page["items"]
        if item.get("state_record_kind") == "review_question"
    }
    relation = next(
        item for item in page["items"] if item.get("state_record_kind") == "review_question_relation"
    )
    assert question_ids == {"q1", "q2"}
    assert relation["parent_question_id"] == "q1"
    assert relation["child_question_id"] == "q2"
    assert relation["both_local_outcomes_present"] is False


def test_child_review_includes_ancestor_but_not_a_sibling_from_another_set() -> None:
    workspace = _workspace()
    workspace["source_sets"][0]["question_ids"] = ["q2"]
    workspace["coverage_questions"][1]["parent_question_id"] = "q1"
    workspace["coverage_questions"].append(
        {
            "question_id": "q3",
            "question": "What else?",
            "parent_question_id": "q1",
            "answer_requirements": [],
        }
    )
    workspace["source_findings"][0]["question_links"] = [
        {"question_id": "q2", "requirement_ids": ["q2:r1"], "rationale": "Cause."}
    ]
    workspace["coverage_outcomes"] = [_outcome("q2", "q2:r1")]

    page = semantic_review_page(
        workspace,
        workspace["source_sets"][0],
        evidence_by_id={"evidence-1": {"evidence_id": "evidence-1", "snippet_text": "Evidence."}},
        cursor=0,
        limit=100,
    )

    question_ids = {
        str(item.get("question_id") or "")
        for item in page["items"]
        if item.get("state_record_kind") == "review_question"
    }
    assert question_ids == {"q1", "q2"}
    assert all(
        item.get("child_question_id") != "q3"
        for item in page["items"]
        if item.get("state_record_kind") == "review_question_relation"
    )


def test_parent_review_includes_its_complete_descendant_tree() -> None:
    workspace = _workspace()
    workspace["source_sets"][0]["question_ids"] = ["q1"]
    workspace["coverage_questions"][1]["parent_question_id"] = "q1"
    workspace["source_findings"][0]["question_links"] = []
    workspace["coverage_outcomes"] = [_outcome("q1", "q1:r1")]

    page = semantic_review_page(
        workspace,
        workspace["source_sets"][0],
        evidence_by_id={"evidence-1": {"evidence_id": "evidence-1", "snippet_text": "Evidence."}},
        cursor=0,
        limit=100,
    )

    question_ids = {
        str(item.get("question_id") or "")
        for item in page["items"]
        if item.get("state_record_kind") == "review_question"
    }
    assert question_ids == {"q1", "q2"}


def test_semantic_review_refuses_to_certify_missing_evidence_text() -> None:
    workspace = _workspace()

    page = semantic_review_page(
        workspace,
        workspace["source_sets"][0],
        evidence_by_id={},
        cursor=0,
        limit=100,
    )

    assert page == {
        "accepted": False,
        "error": "semantic_review_evidence_unavailable",
        "set_id": "set-1",
        "evidence_ids": ["evidence-1"],
        "next_action": "Retry semantic_review after the persisted evidence is readable.",
    }


def test_semantic_review_is_paged_losslessly_and_invalidated_by_semantic_changes() -> None:
    workspace = _workspace()
    evidence_text = "E" * 17_500

    pages = _read_review(workspace, evidence_text=evidence_text)

    assert all(page["output_language"] == "ru" for page in pages)
    assert all("exact completion criterion" in page["review_instruction"] for page in pages)
    assert set_semantic_review_is_current(workspace, workspace["source_sets"][0]) is True
    workspace["source_triage_set_semantic_self_checks"] = {
        "set-1": {
            "contract_version": "v2.semantic_review_ack.1",
            "review_snapshot_id": set_semantic_review_signature(
                workspace,
                workspace["source_sets"][0],
            ),
            "readiness_explanation": "Проверка завершена.",
        }
    }
    assert set_semantic_self_check_is_current(workspace, workspace["source_sets"][0]) is True
    self_check_page = semantic_self_check_page(
        workspace,
        workspace["source_sets"][0],
        cursor=0,
        limit=100,
    )
    assert self_check_page["accepted"] is True
    assert self_check_page["items"][0]["is_current"] is True
    evidence_parts = sorted(
        (
            item
            for page in pages
            for item in page["items"]
            if item.get("state_record_kind") == "review_evidence"
            and item.get("part_kind") == "snippet_part"
        ),
        key=lambda item: item["part_index"],
    )
    assert "".join(item["text"] for item in evidence_parts) == evidence_text

    workspace["source_findings"][0]["statement"] = "Changed bounded finding."
    assert set_semantic_review_is_current(workspace, workspace["source_sets"][0]) is False
    assert set_semantic_self_check_is_current(workspace, workspace["source_sets"][0]) is False
    _read_review(workspace, evidence_text=evidence_text)
    assert set_semantic_review_is_current(workspace, workspace["source_sets"][0]) is True

    workspace["coverage_outcomes"][0]["orientation_summary"] = "Changed outcome."
    assert set_semantic_review_is_current(workspace, workspace["source_sets"][0]) is False


def test_semantic_review_emits_each_finding_and_evidence_once() -> None:
    workspace = _workspace()
    second = dict(workspace["source_findings"][0])
    second["finding_id"] = "finding-2"
    second["statement"] = "Second independent finding."
    workspace["source_findings"].append(second)

    page = semantic_review_page(
        workspace,
        workspace["source_sets"][0],
        evidence_by_id={
            "evidence-1": {
                "evidence_id": "evidence-1",
                "snippet_text": "Shared exact evidence.",
                "locator": "section 1",
            }
        },
        cursor=0,
        limit=100,
    )

    evidence_cores = [
        item
        for item in page["items"]
        if item.get("state_record_kind") == "review_evidence"
        and item.get("part_kind") == "core"
    ]
    encoded = json.dumps(page["items"], ensure_ascii=False)
    assert len(evidence_cores) == 1
    assert evidence_cores[0]["finding_ids"] == ["finding-1", "finding-2"]
    assert encoded.count("One bounded finding supports both questions.") == 1
    assert encoded.count("Second independent finding.") == 1


def test_large_assessment_structure_stays_lossless_in_source_memory_and_compact_in_review() -> None:
    workspace = _workspace()
    structure_items = [
        {
            "kind": "heading",
            "offset": index * 100,
            "label_preview": f"Section {index} " + ("L" * 120),
            "label_chars": 132,
            "label_omitted_chars": 0,
        }
        for index in range(150)
    ]
    workspace["source_assessments"][0]["reading_basis"] = {
        "receipt_count": 3,
        "coverage_summary": "Read the relevant sections in sequence.",
        "unread_regions": "Appendices.",
        "unread_risk_assessment": "Appendices contain reference tables only.",
        "representation_limits": "Text extraction retained headings.",
        "stopping_reason": "Further reading cannot change the bounded finding.",
        "coverage_ledger": {
            "source_ref": "S01",
            "total_chars": 80_000,
            "sequential_ranges": [{"start_offset": 0, "end_offset": 20_000}],
            "sequential_chars_read": 20_000,
            "unread_char_count": 60_000,
            "eof_reached": False,
            "search_ranges": [],
            "search_queries": [],
            "structure": {
                "items": structure_items,
                "counts": {"heading": len(structure_items)},
                "omitted_count": 0,
                "returned_count": len(structure_items),
            },
            "completion_is_model_judgment": True,
        },
    }

    source_set = workspace["source_sets"][0]
    source_pages = []
    cursor = 0
    while True:
        page = source_memory_page(
            workspace,
            source_set,
            source_ref="S01",
            cursor=cursor,
            limit=25,
        )
        assert page["accepted"] is True
        source_pages.append(page)
        next_cursor = str(page["page"]["next_cursor"] or "")
        if not next_cursor:
            break
        cursor = int(next_cursor)
    source_items = [item for page in source_pages for item in page["items"]]
    structure_parts = [
        item
        for item in source_items
        if item.get("part_kind") == "assessment_coverage_structure_items"
    ]
    assert sum(len(item["items"]) for item in structure_parts) == len(structure_items)

    pages = _read_review(workspace, evidence_text="Grounding evidence.")

    assert set_semantic_review_is_current(workspace, workspace["source_sets"][0]) is True
    review_items = [item for page in pages for item in page["items"]]
    assert not any(
        item.get("part_kind") == "assessment_coverage_structure_items"
        for item in review_items
    )
    review_assessment = next(
        item
        for item in review_items
        if item.get("state_record_kind") == "review_source_context"
        and item.get("record_kind") == "source_assessment"
        and item.get("source_ref") == "S01"
    )
    review_structure = review_assessment["reading_basis"]["coverage_ledger"]["structure"]
    assert review_structure["item_count"] == len(structure_items)
    assert review_structure["full_items_location"] == {
        "section": "source_memory",
        "source_ref": "S01",
    }
    assert all(len(json.dumps(page, ensure_ascii=False)) <= 12_000 for page in pages)


def test_large_source_memory_is_readable_as_bounded_pages_without_semantic_truncation() -> None:
    workspace = _workspace()
    long_read = "R" * 5_000
    long_established = "E" * 5_000
    workspace["reading_notes"] = [
        {
            "note_id": "note-1",
            "set_id": "set-1",
            "source_ref": "S01",
            "receipt_refs": ["receipt-1"],
            "what_was_read": long_read,
            "what_was_established": long_established,
            "boundaries": "Actor and period are bounded.",
            "unread_regions": "Appendix.",
            "unread_risk_assessment": "The appendix could qualify table-based conclusions.",
            "question_links": [],
            "doubts": [],
            "next_checks": [],
        }
    ]
    source_set = workspace["source_sets"][0]
    pages = []
    cursor = 0
    while True:
        page = source_memory_page(
            workspace,
            source_set,
            source_ref="S01",
            cursor=cursor,
            limit=25,
        )
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False)) <= 12_000
        pages.append(page)
        next_cursor = str(page["page"]["next_cursor"] or "")
        if not next_cursor:
            break
        cursor = int(next_cursor)

    fields = {
        item["field"]: item["value"]
        for page in pages
        for item in page["items"]
        if item.get("part_kind") == "semantic_field"
    }
    assert fields["what_was_read"] == long_read
    assert fields["what_was_established"] == long_established
    overview = set_overview_page(workspace, source_set, cursor=0, limit=25)
    assert long_read not in json.dumps(overview, ensure_ascii=False)


def test_review_completion_records_reading_only_and_does_not_judge_meaning() -> None:
    workspace = _workspace()
    workspace["source_findings"][0]["statement"] = "A deliberately unsupported semantic claim."

    _read_review(workspace, evidence_text="Evidence that does not support that sentence.")

    assert set_semantic_review_is_current(workspace, workspace["source_sets"][0]) is True


@pytest.mark.asyncio
async def test_review_page_is_counted_only_after_provider_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_model_delivery as delivery

    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    page = semantic_review_page(
        workspace,
        source_set,
        evidence_by_id={
            "evidence-1": {
                "evidence_id": "evidence-1",
                "snippet_text": "Evidence.",
                "locator": "section 1",
            }
        },
        cursor=0,
        limit=100,
    )
    receipt_ref = semantic_review_receipt_ref(
        set_id="set-1",
        snapshot_id=page["review_snapshot_id"],
        cursor=0,
        returned_count=int(page["page"]["returned_count"]),
    )
    stage_semantic_review_page(
        workspace,
        source_set=source_set,
        payload=page,
        receipt_ref=receipt_ref,
    )
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    save_count = 0

    async def save(_db, *, workspace: dict, **_kwargs):
        nonlocal save_count
        save_count += 1
        state["workspace"] = workspace

    monkeypatch.setattr(delivery, "load_source_triage_workspace", load)
    monkeypatch.setattr(delivery, "save_source_triage_workspace", save)

    assert set_semantic_review_is_current(workspace, source_set) is False
    acknowledged = await mark_runtime_delivery_receipts_delivered(
        None,
        generation_run_id="run-1",
        receipt_refs=[receipt_ref],
    )
    assert acknowledged == 1
    assert set_semantic_review_is_current(state["workspace"], source_set) is True
    assert save_count == 1
    assert state["workspace"]["source_triage_semantic_review_deliveries"] == []

    state["workspace"]["source_triage_set_semantic_reviews"] = {}
    acknowledged = await mark_runtime_delivery_receipts_delivered(
        None,
        generation_run_id="run-1",
        receipt_refs=[receipt_ref],
    )
    assert acknowledged == 0
    assert save_count == 1
    assert set_semantic_review_is_current(state["workspace"], source_set) is False


@pytest.mark.asyncio
async def test_question_workbench_tool_stages_the_exact_model_visible_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_model_delivery as model_delivery
    from app.generation_v2 import source_triage_workspace_tools as workspace_tools

    workspace = _workspace()

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, workspace: dict, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_tools, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_tools, "save_source_triage_workspace", save)
    monkeypatch.setattr(model_delivery, "load_source_triage_workspace", load)
    monkeypatch.setattr(model_delivery, "save_source_triage_workspace", save)
    tool = workspace_tools._source_set_page_tool(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-1"),  # type: ignore[arg-type]
        workspace=workspace,
        current_set_id="set-1",
        evidence_toolset=None,
    )

    payload = await tool.handler(
        {
            "section": "question_workbench",
            "set_id": "set-1",
            "question_id": "q1",
            "cursor": "0",
            "limit": 25,
        }
    )

    assert payload["accepted"] is True
    assert payload["workbench_receipt_ref"].startswith("workbench_receipt:")
    assert payload["_runtime_receipt_delivery"] == {
        "receipt_refs": [payload["workbench_receipt_ref"]]
    }
    delivery = workspace["source_triage_question_workbench_deliveries"][0]
    assert delivery["receipt_ref"] == payload["workbench_receipt_ref"]
    assert delivery["returned_count"] == payload["page"]["returned_count"]
    assert question_workbench_is_fully_delivered(
        workspace,
        set_id="set-1",
        question_id="q1",
        snapshot_id=payload["workbench_snapshot_id"],
    ) is False
    acknowledged = await mark_runtime_delivery_receipts_delivered(
        None,
        generation_run_id="run-1",
        receipt_refs=[payload["workbench_receipt_ref"]],
    )
    assert acknowledged == 1
    assert question_workbench_is_fully_delivered(
        workspace,
        set_id="set-1",
        question_id="q1",
        snapshot_id=payload["workbench_snapshot_id"],
    ) is True


@pytest.mark.asyncio
async def test_finding_catalog_tool_reserves_receipt_space_before_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace_tools as workspace_tools

    workspace = _workspace()
    finding = workspace["source_findings"][0]
    finding["statement"] = "У" * 2_400
    finding["support_explanation"] = "Д" * 1_600
    finding["limitations"] = [
        f"Ограничение {index}: " + "л" * 760 for index in range(12)
    ]

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, workspace: dict, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_tools, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_tools, "save_source_triage_workspace", save)
    tool = workspace_tools._source_set_page_tool(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-1"),  # type: ignore[arg-type]
        workspace=workspace,
        current_set_id="set-1",
        evidence_toolset=None,
    )

    payload = await tool.handler(
        {
            "section": "finding_catalog",
            "set_id": "set-1",
            "question_id": "",
            "source_ref": "",
            "cursor": "0",
            "limit": 25,
        }
    )

    assert payload["accepted"] is True
    assert payload["finding_catalog_receipt_ref"].startswith(
        "finding_catalog_receipt:"
    )
    model_payload = {
        key: value
        for key, value in payload.items()
        if key != "_runtime_receipt_delivery"
    }
    assert len(json.dumps(model_payload, ensure_ascii=False)) <= 12_000
    assert payload["page"]["output_budget_limited"] is True


@pytest.mark.asyncio
async def test_delivered_structure_pages_are_lossless_external_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_model_delivery as delivery

    workspace = _workspace()
    workspace["source_structure_pages"] = []
    items = [
        {
            "kind": "heading",
            "label_preview": f"Section {index}",
            "label_chars": 9,
            "label_omitted_chars": 0,
            "start_offset": index * 100,
            "end_offset": (index + 1) * 100,
        }
        for index in range(3)
    ]
    payload = {
        "cursor": "0",
        "next_cursor": "",
        "total_count": 3,
        "returned_count": 3,
        "omitted_count": 0,
        "total_chars": 300,
        "counts": {"headings": 3},
        "items": items,
    }
    receipt_ref = "structure_receipt:test"
    stage_source_structure_page(
        workspace,
        set_id="set-1",
        source_ref="S01",
        source_version_id="version-1",
        content_sha256="hash",
        payload=payload,
        receipt_ref=receipt_ref,
    )
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace

    monkeypatch.setattr(delivery, "load_source_triage_workspace", load)
    monkeypatch.setattr(delivery, "save_source_triage_workspace", save)

    assert source_structure_coverage(workspace, set_id="set-1", source_ref="S01") == {}
    await mark_runtime_delivery_receipts_delivered(
        None,
        generation_run_id="run-1",
        receipt_refs=[receipt_ref],
    )
    coverage = source_structure_coverage(state["workspace"], set_id="set-1", source_ref="S01")
    assert coverage["complete"] is True
    assert coverage["items"] == items
    memory = source_memory_page(
        state["workspace"],
        state["workspace"]["source_sets"][0],
        source_ref="S01",
        cursor=0,
        limit=25,
    )
    restored_items = [
        value
        for part in memory["items"]
        if part.get("part_kind") == "full_record"
        for value in part.get("items") or []
    ]
    assert restored_items == items


@pytest.mark.asyncio
async def test_overlapping_structure_pages_are_deduplicated_in_source_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_model_delivery as delivery

    workspace = _workspace()
    workspace["source_structure_pages"] = []
    first = {
        "cursor": "0",
        "next_cursor": "1",
        "total_count": 2,
        "returned_count": 1,
        "omitted_count": 1,
        "total_chars": 200,
        "counts": {"headings": 2},
        "items": [{"kind": "heading", "label_preview": "A", "start_offset": 0, "end_offset": 100}],
    }
    expanded = {
        **first,
        "next_cursor": "",
        "returned_count": 2,
        "omitted_count": 0,
        "items": [
            first["items"][0],
            {"kind": "heading", "label_preview": "B", "start_offset": 100, "end_offset": 200},
        ],
    }
    for receipt_ref, payload in (("structure_receipt:one", first), ("structure_receipt:two", expanded)):
        stage_source_structure_page(
            workspace,
            set_id="set-1",
            source_ref="S01",
            source_version_id="version-1",
            content_sha256="hash",
            payload=payload,
            receipt_ref=receipt_ref,
        )
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace

    monkeypatch.setattr(delivery, "load_source_triage_workspace", load)
    monkeypatch.setattr(delivery, "save_source_triage_workspace", save)
    await mark_runtime_delivery_receipts_delivered(
        None,
        generation_run_id="run-1",
        receipt_refs=["structure_receipt:one", "structure_receipt:two"],
    )

    memory = source_memory_page(
        state["workspace"],
        state["workspace"]["source_sets"][0],
        source_ref="S01",
        cursor=0,
        limit=25,
    )
    restored_items = [
        value
        for part in memory["items"]
        if part.get("part_kind") == "full_record"
        for value in part.get("items") or []
    ]
    assert [item["label_preview"] for item in restored_items] == ["A", "B"]


def test_large_semantic_self_check_is_readable_as_bounded_lossless_pages() -> None:
    workspace = _workspace()
    source_set = workspace["source_sets"][0]
    checks = [
        {
            "finding_id": f"finding-{index}",
            "judgment": "supported_with_limits",
            "reason": f"Причина {index}: " + ("д" * 1_200),
        }
        for index in range(60)
    ]
    workspace["source_triage_set_semantic_self_checks"] = {
        "set-1": {
            "contract_version": "v2.semantic_self_check.2",
            "review_snapshot_id": set_semantic_review_signature(workspace, source_set),
            "output_language": "ru",
            "finding_checks": checks,
            "finding_link_checks": [],
            "requirement_checks": [],
            "source_reading_checks": [],
            "outcome_checks": [],
            "question_relation_checks": [],
            "overall_conclusion": "Проверка завершена без обрезки полей.",
        }
    }

    pages = []
    cursor = 0
    while True:
        page = semantic_self_check_page(
            workspace,
            source_set,
            cursor=cursor,
            limit=25,
        )
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False)) <= 12_000
        pages.append(page)
        next_cursor = str(page["page"]["next_cursor"] or "")
        if not next_cursor:
            break
        cursor = int(next_cursor)

    restored = [
        item
        for page in pages
        for item in page["items"]
        if item.get("part_kind") == "finding_check"
    ]
    assert [{key: value for key, value in item.items() if key != "part_kind"} for item in restored] == checks
