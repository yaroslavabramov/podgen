from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2.corpus_brief_preview import _page as corpus_brief_preview_page
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.research_workspace_prompt_payloads import current_job_prompt_payload
from app.generation_v2.research_workspace_tools import (
    _full_corpus_brief_page,
    _full_job_question_page,
    build_research_workspace_tools,
)
from app.generation_v2.research_source_map_prompting import (
    source_map_planner_summary,
    source_map_research_page,
    source_map_research_summary,
    source_map_summary_for_job,
)
from app.generation_v2.runtime_tool_output import (
    MODEL_TOOL_OUTPUT_MAX_CHARS,
    model_facing_tool_output,
)
from app.generation_v2.schemas import (
    SourceAssessment,
    SourceCard,
    SourceCluster,
    SourceGapRecord,
    SourceMap,
    SourceRelationship,
    SourceTriageReport,
)
from app.generation_v2.source_triage_workspace_tools import (
    _coverage_question_packet_page,
    _full_canonical_item_page,
    _model_safe_source_finding,
    build_workspace_tools as build_triage_workspace_tools,
    finalizer_review_stream_violations,
)
from app.generation_v2.source_triage_canonical import coverage_synthesis_input_hash
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref


def test_legacy_internal_relation_id_is_not_exposed_as_source_ref() -> None:
    internal_id = str(uuid.uuid4())

    payload = _model_safe_source_finding(
        {
            "finding_id": "finding:one",
            "source_ref": "S01",
            "source_relations": [
                {"related_source_id": internal_id, "relationship": "duplicate"}
            ],
        }
    )

    assert payload["source_relations"] == []
    assert internal_id not in json.dumps(payload)


def test_source_map_gap_context_is_structured_deduped_and_uses_public_refs() -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            SourceCard(source_id=source_id_1, title="First", source_type="web", summary="First source."),
            SourceCard(source_id=source_id_2, title="Second", source_type="web", summary="Second source."),
        ]
    )
    structured_description = f"{source_id_1} leaves one mechanism unresolved."
    source_map = SourceMap(
        source_cards=inventory.source_cards,
        gaps=[structured_description],
        contradictions=[f"{source_id_2} conflicts with the earlier account."],
        gap_records=[
            SourceGapRecord(
                gap_id="gap:internal-hash",
                kind="gap",
                description=structured_description,
                source_ids=[source_id_1],
                evidence_ids=[evidence_id],
            )
        ],
    )

    summary = source_map_research_summary(source_map, inventory, include_evidence_ids=True)
    planner = source_map_planner_summary(source_map, inventory)
    page = source_map_research_page(
        source_map,
        inventory,
        section="source_map_gap_records",
        include_evidence_ids=True,
    )
    job_summary = source_map_summary_for_job(summary, {"source_refs": ["S01"]})

    assert summary["gap_record_count"] == 1
    assert summary["gap_count"] == 2
    assert summary["legacy_gap_count"] == 1
    gap_ref = public_gap_ref("gap:internal-hash")
    assert summary["gap_records"][0] == {
        "gap_ref": gap_ref,
            "kind": "gap",
            "source_refs": ["S01"],
            "question_ids": [],
            "evidence_ids": [evidence_id],
        "evidence_id_count": 1,
        "evidence_id_omitted_count": 0,
        "description_preview": "S01 leaves one mechanism unresolved.",
        "description_chars": len("S01 leaves one mechanism unresolved."),
        "description_truncated": False,
    }
    assert summary["gaps"][0]["gap_preview"] == "S02 conflicts with the earlier account."
    assert "gap_record_index" not in planner
    assert planner["pages"]["source_map_gap_records"]["total_count"] == 1
    assert page["items"][0]["gap_ref"] == gap_ref
    assert page["items"][0]["description"] == "S01 leaves one mechanism unresolved."
    assert job_summary["gap_records"][0]["evidence_ids"] == [evidence_id]
    model_json = json.dumps(
        {"summary": summary, "planner": planner, "page": page, "job": job_summary}
    )
    assert "gap:internal-hash" not in model_json
    assert source_id_1 not in model_json
    assert source_id_2 not in model_json


def test_source_map_rejects_duplicate_structured_gap_ids() -> None:
    record = SourceGapRecord(gap_id="gap:duplicate", description="First representation.")

    with pytest.raises(ValueError, match="duplicate gap record ids"):
        SourceMap(
            gap_records=[
                record,
                record.model_copy(update={"description": "Conflicting representation."}),
            ]
        )


@pytest.mark.asyncio
async def test_corpus_brief_gap_page_hides_internal_id_and_only_job_mode_gets_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    long_description = f"The account in {source_id} remains disputed. " + ("Full detail. " * 120)
    inventory = SourceMap(
        source_cards=[SourceCard(source_id=source_id, title="First", source_type="web", summary="First.")]
    )
    workspace = {
        "corpus_brief": {
            "gap_records": [
                {
                    "gap_id": "gap:internal-hash",
                    "kind": "contradiction",
                    "description": long_description,
                    "source_ids": [source_id],
                    "evidence_ids": [evidence_id],
                }
            ]
        }
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)

    async def page_for(mode: str) -> dict:
        tools = build_research_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            workspace=workspace,
            source_inventory=inventory,
            mode=mode,
        )
        page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")
        return await page_tool.handler(
            {"section": "corpus_brief_gap_records", "cursor": "0", "limit": 10}
        )

    planner_page = await page_for("planner")
    job_page = await page_for("job")

    assert planner_page["items"][0] == {
        "kind": "contradiction",
        "description": long_description.replace(source_id, "S01"),
        "source_refs": ["S01"],
        "gap_ref": public_gap_ref("gap:internal-hash"),
        "evidence_count": 1,
    }
    assert job_page["items"][0]["evidence_ids"] == [evidence_id]
    assert "gap_id" not in job_page["items"][0]


def _payload(items: list[dict[str, str]]) -> dict:
    return {
        "accepted": True,
        "section": "coverage_questions",
        "items": items,
        "page": {
            "total_count": len(items),
            "returned_count": len(items),
            "omitted_count": 0,
            "cursor": "0",
            "next_cursor": "",
        },
    }


def test_triage_canonical_page_honors_requested_limit() -> None:
    workspace = {
        "coverage_questions": [
            {
                "question_id": f"q-{index}",
                "question": f"Question {index}",
                "kind": "context",
                "importance": "supporting",
                "rationale": f"Rationale {index}",
            }
            for index in range(5)
        ]
    }

    page = _full_canonical_item_page(
        workspace,
        section="coverage_questions",
        cursor=0,
        limit=3,
    )

    assert [item["question_id"] for item in page["items"]] == ["q-0", "q-1", "q-2"]
    assert page["page"]["returned_count"] == 3
    assert page["page"]["next_cursor"] == "3"
    assert page["page"]["output_budget_limited"] is False


def test_triage_synthesis_page_exposes_freshness_without_internal_hash_or_revision() -> None:
    workspace = {
        "triage_semantic_revision": 2,
        "coverage_questions": [{"question_id": "q-main", "question": "What happened?"}],
        "source_sets": [],
        "source_assessments": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [
            {
                "outcome_id": "synthesis:q-main",
                "question_id": "q-main",
                "status": "missing",
                "missing_detail": "The corpus does not establish the answer.",
                "next_action": "source_acquisition",
                "input_revision": 2,
            }
        ],
    }
    workspace["coverage_syntheses"][0]["input_hash"] = coverage_synthesis_input_hash(
        workspace,
        "q-main",
    )

    page = _full_canonical_item_page(
        workspace,
        section="coverage_syntheses",
        cursor=0,
        limit=1,
    )

    synthesis = page["items"][0]
    assert synthesis["input_current"] is True
    assert "input_hash" not in synthesis
    assert "input_revision" not in synthesis


def test_research_workspace_page_honors_requested_limit() -> None:
    workspace = {
        "investigation_jobs": [
            {"job_id": "job-main", "questions": [f"Question {index}" for index in range(5)]}
        ]
    }

    page = _full_job_question_page(
        workspace,
        cursor=0,
        job_id="job-main",
        limit=3,
    )

    assert [item["question"] for item in page["items"]] == ["Question 0", "Question 1", "Question 2"]
    assert page["page"]["returned_count"] == 3
    assert page["page"]["next_cursor"] == "3"


def test_model_page_reduces_batch_without_cutting_records() -> None:
    records = [{"text": char * 4500} for char in "abc"]

    page = fit_model_page(_payload(records), requested_limit=3)

    assert page["accepted"] is True
    assert page["items"] == records[:2]
    assert page["page"]["returned_count"] == 2
    assert page["page"]["next_cursor"] == "2"
    assert page["page"]["output_budget_limited"] is True
    assert model_facing_tool_output("list_triage_source_sets", page) is page


def test_model_page_rejects_one_oversized_record_instead_of_truncating_it() -> None:
    page = fit_model_page(_payload([{"text": "x" * 13000}]), requested_limit=1)

    assert page["accepted"] is False
    assert page["error"] == "workspace_item_exceeds_model_read_contract"
    assert page["section"] == "coverage_questions"
    assert "items" not in page


@pytest.mark.asyncio
async def test_triage_handler_pages_ten_whole_records_without_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    questions = [
        {
            "question_id": f"q-{index}",
            "question": f"Question {index}: " + ("full semantic text " * 12),
            "kind": "context",
            "importance": "supporting",
            "rationale": f"Rationale {index}: " + ("persisted reasoning " * 12),
        }
        for index in range(10)
    ]
    workspace = {"coverage_questions": questions}

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="planner",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    pages = [
        await page_tool.handler(
            {"section": "coverage_questions", "cursor": str(cursor), "limit": 3}
        )
        for cursor in (0, 3, 6, 9)
    ]

    assert [page["page"]["returned_count"] for page in pages] == [3, 3, 3, 1]
    assert [page["page"]["next_cursor"] for page in pages] == ["3", "6", "9", ""]
    returned = [item for page in pages for item in page["items"]]
    assert [item["question_id"] for item in returned] == [item["question_id"] for item in questions]
    assert [item["question"] for item in returned] == [item["question"] for item in questions]
    assert [item["rationale"] for item in returned] == [item["rationale"] for item in questions]
    for page in pages:
        model_facing_tool_output("list_triage_source_sets", page)


@pytest.mark.asyncio
async def test_only_finalizer_reads_current_question_and_finding_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    workspace = {
        "schema_version": "source_triage_workspace.v3",
        "coverage_questions": [
            {
                "question_id": "q-main",
                "question": "Why did it happen?",
                "kind": "cause",
                "importance": "critical",
                "rationale": "The request asks for a cause.",
                "answer_requirements": [
                    {
                        "requirement_id": "q-main:mechanism",
                        "description": "Explain the mechanism.",
                        "rationale": "A cause needs a connecting mechanism.",
                    }
                ],
            }
        ],
        "source_findings": [
            {
                "finding_id": "finding:S01:one",
                "set_id": "set-main",
                "source_ref": "S01",
                "source_id": "source-1",
                "statement": "The source establishes the first mechanism.",
                "evidence_ids": ["evidence-1"],
                "scope": {"actor": "Actor", "period": "Period", "qualifiers": []},
                "limitations": [],
                "question_links": [
                    {
                        "question_id": "q-main",
                        "requirement_ids": ["q-main:mechanism"],
                    }
                ],
                "source_relations": [],
                "semantic_revision": 1,
            }
        ],
        "source_sets": [
            {
                "set_id": "set-main",
                "source_refs": ["S01"],
                "question_ids": ["q-main"],
                "planned_question_ids": ["q-main"],
                "cross_question_ids": [],
                "evidence_ids": ["evidence-1"],
            }
        ],
        "source_assessments": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [],
        "triage_semantic_revision": 1,
    }
    save_calls = 0

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **_kwargs):
        nonlocal save_calls
        save_calls += 1
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)

    async def page_all(mode: str, section: str) -> None:
        tools = build_triage_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            workspace=workspace,
            inventory=SourceMap(),
            mode=mode,
            current_set_id="set-main" if mode == "set" else None,
        )
        page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")
        cursor = "0"
        while True:
            page = await page_tool.handler(
                {"section": section, "cursor": cursor, "limit": 1}
            )
            assert page["accepted"] is True
            cursor = page["page"]["next_cursor"]
            if not cursor:
                return

    await page_all("planner", "coverage_questions")
    await page_all("set", "source_findings")
    assert save_calls == 0
    assert "source_triage_finalizer_reads" not in workspace
    assert finalizer_review_stream_violations(workspace)

    finalizer_tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    update_links = next(
        tool for tool in finalizer_tools if tool.name == "update_source_finding_links"
    )
    update_before_read = await update_links.handler(
        {
            "finding_id": "finding:S01:one",
            "question_links": [
                {
                    "question_id": "q-main",
                    "requirement_ids": ["q-main:mechanism"],
                    "rationale": "The finding directly supports the mechanism.",
                }
            ],
        }
    )
    assert update_before_read["accepted"] is True
    assert update_before_read["changed"] is True
    assert finalizer_review_stream_violations(workspace)

    await page_all("finalizer", "coverage_questions")
    missing_findings = finalizer_review_stream_violations(workspace)[0]["sections"]
    assert [item["section"] for item in missing_findings] == ["source_findings"]
    assert missing_findings[0]["missing_part_count"] > 0
    assert missing_findings[0]["missing_owner_refs"] == ["finding:S01:one"]
    await page_all("finalizer", "source_findings")
    assert finalizer_review_stream_violations(workspace) == []
    assert save_calls > 0

    update_after_read = await update_links.handler(
        {
            "finding_id": "finding:S01:one",
            "question_links": [
                {
                    "question_id": "q-main",
                    "requirement_ids": ["q-main:mechanism"],
                    "rationale": "The reviewed finding directly establishes the mechanism.",
                }
            ],
        }
    )
    assert update_after_read["accepted"] is True
    assert update_after_read["changed"] is True
    assert finalizer_review_stream_violations(workspace) == []

    workspace["source_findings"][0]["statement"] = "The current finding changed after review."
    violations = finalizer_review_stream_violations(workspace)
    assert violations[0]["sections"][0]["section"] == "source_findings"

    await page_all("finalizer", "source_findings")
    assert finalizer_review_stream_violations(workspace) == []


@pytest.mark.asyncio
async def test_triage_handler_pages_33_assessments_with_a_real_requested_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    assessments = [
        {
            "set_id": "set-main",
            "source_ref": f"S{index + 1:02d}",
            "processing_status": "resolved",
            "representation_status": "usable",
            "representation_reason": f"Source {index + 1} contains readable primary text.",
            "relevance": "high",
            "relevance_reason": f"Source {index + 1} directly addresses the coverage question.",
            "authority": "primary",
            "authority_reason": "Published by the institution responsible for the policy.",
            "uniqueness": "distinct",
            "uniqueness_reason": f"Source {index + 1} contributes a distinct dated record.",
            "disposition": "keep",
            "disposition_reason": "Keep for final synthesis.",
            "coverage_question_ids": ["q-main"],
            "editorial_functions": ["causal_evidence"],
            "evidence_ids": [f"evidence-{index + 1}"],
            "limitations": [],
            "confidence": "high",
        }
        for index in range(33)
    ]
    workspace = {"source_assessments": assessments}

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    limit_schema = page_tool.parameters["properties"]["limit"]
    assert limit_schema["default"] == 20
    assert limit_schema["maximum"] == 25

    pages = []
    cursor = "0"
    while True:
        page = await page_tool.handler(
            {"section": "source_assessments", "cursor": cursor, "limit": 25}
        )
        model_facing_tool_output("list_triage_source_sets", page)
        pages.append(page)
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break

    assert pages[0]["page"]["returned_count"] > 3
    returned = [item for page in pages for item in page["items"]]
    assert [item["source_ref"] for item in returned] == [
        item["source_ref"] for item in assessments
    ]
    assert [item["relevance_reason"] for item in returned] == [
        item["relevance_reason"] for item in assessments
    ]


@pytest.mark.asyncio
async def test_triage_finalizer_pages_complete_question_packets_with_derived_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {
                "question_id": "q-main",
                "question": "Why did the system persist?",
                "kind": "cause",
                "importance": "critical",
                "rationale": "This is the user's central question.",
            }
        ],
        "source_assessments": [
            {"source_id": source_1, "source_ref": "S01"},
            {"source_id": source_2, "source_ref": "S02"},
        ],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome:q-main:set-1",
                "question_id": "q-main",
                "set_id": "set-1",
                "status": "partial",
                "source_refs": ["S01"],
                "evidence_ids": ["evidence-1"],
                "orientation_summary": "The first source establishes the initial mechanism.",
                "missing_detail": "Persistence is not yet explained.",
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.8,
            },
            {
                "outcome_id": "outcome:q-main:set-2",
                "question_id": "q-main",
                "set_id": "set-2",
                "status": "covered",
                "source_ids": [source_2],
                "evidence_ids": ["evidence-2"],
                "orientation_summary": "The second source explains persistence.",
                "missing_detail": "",
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.9,
            },
        ],
        "coverage_syntheses": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[
                SourceCard(source_id=source_1, title="First", source_type="web", summary="First."),
                SourceCard(source_id=source_2, title="Second", source_type="web", summary="Second."),
            ]
        ),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    page = await page_tool.handler(
        {"section": "coverage_question_packets", "cursor": "0", "limit": 20}
    )

    assert page["accepted"] is True
    assert page["packet_contract"] == "ordered_parts_v1"
    assert page["page"]["unit"] == "packet_part"
    assert page["page"]["returned_count"] == 4
    assert all(item["owner_ref"] for item in page["items"])
    question_part = page["items"][0]
    assert question_part["part_kind"] == "question"
    assert question_part["owner_ref"] == "coverage_question:q-main"
    assert question_part["question"]["question_id"] == "q-main"
    assert question_part["derived_lineage"]["for_covered_partial_or_conflicted"] == {
        "outcome_count": 2,
        "source_count": 2,
        "evidence_count": 2,
    }
    synthesis_part = page["items"][1]
    assert synthesis_part["part_kind"] == "existing_synthesis"
    assert synthesis_part["owner_ref"] == "coverage_question:q-main:existing_synthesis"
    assert synthesis_part["existing_synthesis"] is None
    outcome_parts = [item for item in page["items"] if item["part_kind"] == "per_set_outcome"]
    assert [item["owner_ref"] for item in outcome_parts] == [
        "outcome:q-main:set-1",
        "outcome:q-main:set-2",
    ]
    assert [item["per_set_outcome"]["set_id"] for item in outcome_parts] == ["set-1", "set-2"]
    assert [item["per_set_outcome"]["evidence_ids"] for item in outcome_parts] == [
        ["evidence-1"],
        ["evidence-2"],
    ]
    assert "coverage_question_packets" in page_tool.parameters["properties"]["section"]["enum"]


@pytest.mark.asyncio
async def test_question_packets_expose_factual_cross_question_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {
                "question_id": "q-b",
                "question": "What mechanism was established?",
                "kind": "mechanism",
                "importance": "critical",
                "rationale": "A sibling question may contain relevant established material.",
            },
            {
                "question_id": "q-a",
                "question": "Why did the arrangement persist?",
                "kind": "cause",
                "importance": "critical",
                "rationale": "The causal account currently has a specific gap.",
            },
        ],
        "source_assessments": [],
        "source_sets": [],
        # Deliberately reversed: packet and overlap ordering must not follow mutation order.
        "coverage_outcomes": [
            {
                "question_id": "q-b",
                "set_id": "set-shared",
                "status": "covered",
                "source_refs": ["S02", "S01"],
                "evidence_ids": ["evidence-b", "evidence-shared"],
                "orientation_summary": "The source establishes the operating mechanism.",
                "missing_detail": "",
                "confidence": 0.9,
            },
            {
                "question_id": "q-a",
                "set_id": "set-shared",
                "status": "partial",
                "source_refs": ["S01", "S03"],
                "evidence_ids": ["evidence-shared", "evidence-a"],
                "orientation_summary": "The source establishes the initial arrangement.",
                "missing_detail": "The persistence mechanism is not reconciled here.",
                "confidence": 0.7,
            },
        ],
        "coverage_syntheses": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    page = await page_tool.handler(
        {"section": "coverage_question_packets", "cursor": "0", "limit": 20}
    )

    parts = page["items"]
    assert [part["packet_ref"] for part in parts if part["part_kind"] == "question"] == [
        "coverage_question:q-a",
        "coverage_question:q-b",
    ]
    overlap_index = next(
        index for index, part in enumerate(parts) if part["part_kind"] == "cross_question_overlap"
    )
    referenced_outcome_indexes = {
        part["per_set_outcome"]["outcome_ref"]: index
        for index, part in enumerate(parts)
        if part["part_kind"] == "per_set_outcome"
    }
    overlap = parts[overlap_index]["cross_question_overlap"]
    assert overlap == {
        "overlap_ref": overlap["overlap_ref"],
        "question_refs": ["coverage_question:q-a", "coverage_question:q-b"],
        "outcome_refs": ["outcome:q-a:set-shared", "outcome:q-b:set-shared"],
        "shared_set_ids": ["set-shared"],
        "source_refs": ["S01"],
        "evidence_ids": ["evidence-shared"],
    }
    assert overlap["overlap_ref"].startswith("cross_overlap:")
    assert sum(part["part_kind"] == "cross_question_overlap" for part in parts) == 1
    assert all(referenced_outcome_indexes[ref] < overlap_index for ref in overlap["outcome_refs"])
    established = next(
        part["per_set_outcome"]
        for part in parts
        if part["part_kind"] == "per_set_outcome"
        and part["per_set_outcome"]["outcome_ref"] == "outcome:q-b:set-shared"
    )
    assert established["orientation_summary"] == "The source establishes the operating mechanism."


@pytest.mark.asyncio
async def test_cross_question_overlap_groups_shared_membership_instead_of_pairwise_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    question_ids = [f"q-{index:02d}" for index in range(40)]
    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {"question_id": question_id, "question": f"Question {question_id}"}
            for question_id in reversed(question_ids)
        ],
        "source_assessments": [],
        "source_sets": [],
        "coverage_outcomes": [
            {
                "question_id": question_id,
                "set_id": "set-shared",
                "status": "missing",
                "source_refs": [],
                "evidence_ids": [],
                "missing_detail": f"No answer for {question_id} in this local outcome.",
                "confidence": 0.7,
            }
            for question_id in reversed(question_ids)
        ],
        "coverage_syntheses": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    pages = []
    cursor = "0"
    while True:
        page = await page_tool.handler(
            {"section": "coverage_question_packets", "cursor": cursor, "limit": 25}
        )
        assert page["accepted"] is True
        pages.append(page)
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break

    parts = [item for page in pages for item in page["items"]]
    overlaps = [item for item in parts if item["part_kind"] == "cross_question_overlap"]
    assert len(overlaps) == 1
    overlap = overlaps[0]
    assert overlap["packet_ref"] == "coverage_question:q-39"
    assert overlap["cross_question_overlap"]["question_refs"] == [
        f"coverage_question:{question_id}" for question_id in question_ids
    ]
    assert overlap["cross_question_overlap"]["outcome_refs"] == [
        f"outcome:{question_id}:set-shared" for question_id in question_ids
    ]
    assert overlap["cross_question_overlap"]["shared_set_ids"] == ["set-shared"]
    assert overlap["cross_question_overlap"]["source_refs"] == []
    assert overlap["cross_question_overlap"]["evidence_ids"] == []


@pytest.mark.asyncio
async def test_cross_question_overlap_keeps_distinct_memberships_in_distinct_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {"question_id": question_id, "question": f"Question {question_id}"}
            for question_id in ("q-3", "q-1", "q-2")
        ],
        "source_assessments": [],
        "source_sets": [],
        "coverage_outcomes": [
            {
                "question_id": "q-1",
                "set_id": "set-common",
                "status": "partial",
                "source_refs": ["S12"],
                "evidence_ids": [],
                "missing_detail": "Local gap one.",
            },
            {
                "question_id": "q-2",
                "set_id": "set-common",
                "status": "partial",
                "source_refs": ["S12"],
                "evidence_ids": ["evidence-23"],
                "missing_detail": "Local gap two.",
            },
            {
                "question_id": "q-3",
                "set_id": "set-common",
                "status": "partial",
                "source_refs": [],
                "evidence_ids": ["evidence-23"],
                "missing_detail": "Local gap three.",
            },
        ],
        "coverage_syntheses": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    page = await page_tool.handler(
        {"section": "coverage_question_packets", "cursor": "0", "limit": 20}
    )

    overlaps = [
        item
        for item in page["items"]
        if item["part_kind"] == "cross_question_overlap"
    ]
    assert len(overlaps) == 3
    records = [item["cross_question_overlap"] for item in overlaps]
    common_set = next(record for record in records if record["shared_set_ids"])
    shared_source = next(record for record in records if record["source_refs"])
    shared_evidence = next(record for record in records if record["evidence_ids"])
    assert common_set["outcome_refs"] == [
        "outcome:q-1:set-common",
        "outcome:q-2:set-common",
        "outcome:q-3:set-common",
    ]
    assert shared_source["outcome_refs"] == [
        "outcome:q-1:set-common",
        "outcome:q-2:set-common",
    ]
    assert shared_evidence["outcome_refs"] == [
        "outcome:q-2:set-common",
        "outcome:q-3:set-common",
    ]
    assert [value for record in records for value in record["shared_set_ids"]] == [
        "set-common"
    ]
    assert [value for record in records for value in record["source_refs"]] == ["S12"]
    assert [value for record in records for value in record["evidence_ids"]] == [
        "evidence-23"
    ]
    item_by_overlap_ref = {
        item["cross_question_overlap"]["overlap_ref"]: item for item in overlaps
    }
    assert item_by_overlap_ref[common_set["overlap_ref"]]["packet_ref"] == "coverage_question:q-3"
    assert item_by_overlap_ref[shared_source["overlap_ref"]]["packet_ref"] == "coverage_question:q-2"
    assert item_by_overlap_ref[shared_evidence["overlap_ref"]]["packet_ref"] == "coverage_question:q-3"


@pytest.mark.asyncio
async def test_cross_question_overlap_splits_long_shared_lineage_without_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    source_refs = [f"S{index:03d}" for index in range(100)]
    evidence_ids = [f"evidence-{index:04d}-{'x' * 24}" for index in range(700)]
    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {"question_id": "q-a", "question": "Question A"},
            {"question_id": "q-b", "question": "Question B"},
        ],
        "source_assessments": [],
        "source_sets": [],
        "coverage_outcomes": [
            {
                "question_id": question_id,
                "set_id": "set-shared",
                "status": "partial",
                "source_refs": source_refs,
                "evidence_ids": evidence_ids,
                "orientation_summary": f"Established material for {question_id}.",
                "missing_detail": f"Remaining gap for {question_id}.",
                "confidence": 0.8,
            }
            for question_id in ("q-b", "q-a")
        ],
        "coverage_syntheses": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    pages = []
    cursor = "0"
    while True:
        page = await page_tool.handler(
            {"section": "coverage_question_packets", "cursor": cursor, "limit": 25}
        )
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False, default=str)) <= MODEL_TOOL_OUTPUT_MAX_CHARS
        pages.append(page)
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break

    parts = [item for page in pages for item in page["items"]]
    overlap = next(item for item in parts if item["part_kind"] == "cross_question_overlap")
    overlap_ref = overlap["cross_question_overlap"]["overlap_ref"]
    assert overlap["question_ref_count"] == 2
    assert overlap["outcome_ref_count"] == 2
    assert overlap["shared_set_id_count"] == 1
    assert overlap["source_ref_count"] == len(source_refs)
    assert overlap["evidence_id_count"] == len(evidence_ids)
    overlap_lineage = [
        item
        for item in parts
        if item["part_kind"] == "lineage"
        and item["owner_kind"] == "cross_question_overlap"
    ]
    assert len(overlap_lineage) > 1
    assert [value for item in overlap_lineage for value in item["question_refs"]] == [
        "coverage_question:q-a",
        "coverage_question:q-b",
    ]
    assert [value for item in overlap_lineage for value in item["outcome_refs"]] == [
        "outcome:q-a:set-shared",
        "outcome:q-b:set-shared",
    ]
    assert [value for item in overlap_lineage for value in item["shared_set_ids"]] == [
        "set-shared"
    ]
    assert [value for item in overlap_lineage for value in item["source_refs"]] == source_refs
    assert [value for item in overlap_lineage for value in item["evidence_ids"]] == evidence_ids
    assert all(item["owner_ref"] == overlap_ref for item in overlap_lineage)


@pytest.mark.asyncio
async def test_large_question_packet_continues_without_cutting_semantic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    outcome_summary = "O" * 1600
    outcome_missing = "M" * 1200
    synthesis_summary = "S" * 2400
    synthesis_missing = "G" * 1600
    outcomes = [
        {
            "outcome_id": f"outcome:q-main:set-{index}",
            "question_id": "q-main",
            "set_id": f"set-{index}",
            "status": "partial",
            "source_refs": [f"S{index:02d}"],
            "evidence_ids": [f"evidence-{index}"],
            "orientation_summary": outcome_summary,
            "missing_detail": outcome_missing,
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.8,
        }
        for index in range(5)
    ]
    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {
                "question_id": "q-main",
                "question": "Q" * 280,
                "kind": "cause",
                "importance": "critical",
                "rationale": "R" * 800,
            }
        ],
        "source_assessments": [],
        "source_sets": [],
        "coverage_outcomes": outcomes,
        "coverage_syntheses": [
            {
                "outcome_id": "synthesis:q-main",
                "question_id": "q-main",
                "status": "partial",
                "source_refs": [f"S{index:02d}" for index in range(5)],
                "evidence_ids": [f"evidence-{index}" for index in range(5)],
                "orientation_summary": synthesis_summary,
                "missing_detail": synthesis_missing,
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.8,
                "next_action": "research",
                "input_revision": 1,
            }
        ],
    }
    workspace["coverage_syntheses"][0]["input_hash"] = coverage_synthesis_input_hash(
        workspace,
        "q-main",
    )

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    pages = []
    cursor = "0"
    while True:
        page = await page_tool.handler(
            {"section": "coverage_question_packets", "cursor": cursor, "limit": 20}
        )
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False, default=str)) <= MODEL_TOOL_OUTPUT_MAX_CHARS
        pages.append(page)
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break

    assert len(pages) > 1
    parts = [item for page in pages for item in page["items"]]
    assert parts[0]["part_kind"] == "question"
    assert parts[0]["question"]["question"] == "Q" * 280
    synthesis_parts = [item for item in parts if item["part_kind"] == "existing_synthesis"]
    assert synthesis_parts[0]["existing_synthesis"]["orientation_summary"] == synthesis_summary
    assert synthesis_parts[0]["existing_synthesis"]["missing_detail"] == synthesis_missing
    outcome_parts = [item for item in parts if item["part_kind"] == "per_set_outcome"]
    assert len(outcome_parts) == 5
    assert all(item["per_set_outcome"]["orientation_summary"] == outcome_summary for item in outcome_parts)
    assert all(item["per_set_outcome"]["missing_detail"] == outcome_missing for item in outcome_parts)
    assert parts[0]["derived_lineage"]["for_covered_partial_or_conflicted"] == {
        "outcome_count": 5,
        "source_count": 5,
        "evidence_count": 5,
    }


@pytest.mark.asyncio
async def test_question_packet_large_lineage_parts_use_the_real_page_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    source_refs = [f"S{index:03d}" for index in range(100)]
    evidence_ids = [f"evidence-{index:04d}-{'x' * 24}" for index in range(700)]
    workspace = {
        "triage_semantic_revision": 1,
        "coverage_questions": [
            {
                "question_id": "q-main",
                "question": "Q" * 280,
                "kind": "cause",
                "importance": "critical",
                "rationale": "R" * 800,
            }
        ],
        "source_assessments": [],
        "source_sets": [],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome:q-main:set-main",
                "question_id": "q-main",
                "set_id": "set-main",
                "status": "partial",
                "source_refs": source_refs,
                "evidence_ids": evidence_ids,
                "orientation_summary": "O" * 1600,
                "missing_detail": "M" * 1200,
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.8,
            }
        ],
        "coverage_syntheses": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    cursor_three = await page_tool.handler(
        {"section": "coverage_question_packets", "cursor": "3", "limit": 20}
    )
    assert cursor_three["accepted"] is True
    assert len(json.dumps(cursor_three, ensure_ascii=False, default=str)) <= MODEL_TOOL_OUTPUT_MAX_CHARS

    pages = []
    cursor = "0"
    while True:
        page = await page_tool.handler(
            {"section": "coverage_question_packets", "cursor": cursor, "limit": 20}
        )
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False, default=str)) <= MODEL_TOOL_OUTPUT_MAX_CHARS
        pages.append(page)
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break

    parts = [item for page in pages for item in page["items"]]
    outcome_part = next(item for item in parts if item["part_kind"] == "per_set_outcome")
    assert outcome_part["per_set_outcome"]["orientation_summary"] == "O" * 1600
    assert outcome_part["per_set_outcome"]["missing_detail"] == "M" * 1200
    lineage_parts = [item for item in parts if item["part_kind"] == "lineage"]
    assert [value for item in lineage_parts for value in item["source_refs"]] == source_refs
    assert [value for item in lineage_parts for value in item["evidence_ids"]] == evidence_ids


@pytest.mark.parametrize("section", ["source_assessments", "coverage_outcomes"])
def test_mutable_canonical_pages_keep_identity_order_after_upsert(section: str) -> None:
    def records(order: list[str]) -> list[dict[str, object]]:
        if section == "source_assessments":
            return [
                {
                    "assessment_id": f"assessment:{identity}",
                    "source_ref": identity,
                    "relevance_reason": f"record {identity}",
                }
                for identity in order
            ]
        return [
            {
                "outcome_id": f"outcome:q-main:{identity}",
                "question_id": "q-main",
                "set_id": identity,
                "status": "missing",
                "missing_detail": f"record {identity}",
            }
            for identity in order
        ]

    workspace = {section: records(["A", "B", "C", "D"])}
    first = _full_canonical_item_page(workspace, section=section, cursor=0, limit=2)
    workspace[section] = records(["B", "C", "D", "A"])
    second = _full_canonical_item_page(workspace, section=section, cursor=2, limit=2)

    identity_field = "source_ref" if section == "source_assessments" else "set_id"
    assert [item[identity_field] for item in first["items"]] == ["A", "B"]
    assert [item[identity_field] for item in second["items"]] == ["C", "D"]


@pytest.mark.asyncio
async def test_triage_handler_reduces_large_assessment_pages_without_cutting_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    assessments = [
        {
            "set_id": "set-main",
            "source_ref": f"S{index + 1:02d}",
            "processing_status": "resolved",
            "representation_status": "usable",
            "representation_reason": "Readable source.",
            "relevance": "high",
            "relevance_reason": f"Source {index + 1}: " + ("full semantic detail " * 140),
            "authority": "primary",
            "authority_reason": "Primary record.",
            "uniqueness": "distinct",
            "uniqueness_reason": "Distinct contribution.",
            "disposition": "keep",
            "disposition_reason": "Keep.",
            "coverage_question_ids": ["q-main"],
            "editorial_functions": ["causal_evidence"],
            "evidence_ids": [f"evidence-{index + 1}"],
            "limitations": [],
            "confidence": "high",
        }
        for index in range(7)
    ]
    workspace = {"source_assessments": assessments}

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")

    pages = []
    cursor = "0"
    while True:
        page = await page_tool.handler(
            {"section": "source_assessments", "cursor": cursor, "limit": 25}
        )
        model_facing_tool_output("list_triage_source_sets", page)
        pages.append(page)
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break

    assert pages[0]["page"]["returned_count"] < len(assessments)
    assert pages[0]["page"]["output_budget_limited"] is True
    returned = [item for page in pages for item in page["items"]]
    assert [item["source_ref"] for item in returned] == [
        item["source_ref"] for item in assessments
    ]
    assert [item["relevance_reason"] for item in returned] == [
        item["relevance_reason"] for item in assessments
    ]


@pytest.mark.asyncio
async def test_research_handler_pages_current_job_whole_records_without_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    questions = [f"Question {index}: " + ("full semantic text " * 12) for index in range(10)]
    observations = [
        {
            "source_refs": ["S01"],
            "evidence_ids": [f"evidence-{index}"],
            "note": f"Observation {index}: " + ("full persisted note " * 12),
            "pending_questions": [f"Pending {index}: " + ("full pending text " * 8)],
        }
        for index in range(10)
    ]
    workspace = {
        "current_job": {"job_id": "job-main"},
        "investigation_jobs": [
            {
                "job_id": "job-main",
                "source_refs": ["S01"],
                "questions": questions,
                "observations": observations,
                "status": "in_progress",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="job",
        current_job_id="job-main",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")

    question_pages = [
        await page_tool.handler(
            {"section": "current_job_questions", "cursor": str(cursor), "limit": 3}
        )
        for cursor in (0, 3, 6, 9)
    ]
    observation_pages = [
        await page_tool.handler(
            {"section": "current_job_observations", "cursor": str(cursor), "limit": 3}
        )
        for cursor in (0, 3, 6, 9)
    ]

    assert [page["page"]["returned_count"] for page in question_pages] == [3, 3, 3, 1]
    assert [page["page"]["next_cursor"] for page in question_pages] == ["3", "6", "9", ""]
    returned_questions = [item for page in question_pages for item in page["items"]]
    assert [item["question"] for item in returned_questions] == questions
    assert all("question_preview" not in item for item in returned_questions)

    assert [page["page"]["returned_count"] for page in observation_pages] == [3, 3, 3, 1]
    assert [page["page"]["next_cursor"] for page in observation_pages] == ["3", "6", "9", ""]
    returned_observations = [item for page in observation_pages for item in page["items"]]
    assert [item["note"] for item in returned_observations] == [item["note"] for item in observations]
    assert [item["pending_questions"] for item in returned_observations] == [
        item["pending_questions"] for item in observations
    ]
    assert [item["evidence_ids"] for item in returned_observations] == [
        item["evidence_ids"] for item in observations
    ]
    assert all("note_preview" not in item for item in returned_observations)
    for page in [*question_pages, *observation_pages]:
        model_facing_tool_output("list_research_workspace_items", page)


@pytest.mark.asyncio
async def test_full_semantic_handlers_keep_all_nested_evidence_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as research_workspace_api
    from app.generation_v2 import source_triage_workspace as triage_workspace_api

    evidence_ids = [f"evidence-{index}" for index in range(16)]
    source_id = str(uuid.uuid4())
    triage_workspace = {
        "current_set": {"set_id": "set-main"},
        "source_sets": [
            {
                "set_id": "set-main",
                "label": "Main set",
                "purpose": "Map the source.",
                "source_refs": ["S01"],
                "question_ids": ["q-main"],
                "questions": ["What does the source establish?"],
                "status": "in_progress",
                "evidence_ids": evidence_ids,
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": evidence_ids,
                        "note": "Complete persisted observation.",
                        "pending_questions": [],
                    }
                ],
            }
        ],
    }
    research_workspace = {
        "current_job": {"job_id": "job-main"},
        "investigation_jobs": [
            {
                "job_id": "job-main",
                "source_refs": ["S01"],
                "questions": ["What changed?"],
                "question_outcomes": [
                    {
                        "question": "What changed?",
                        "status": "resolved",
                        "note": "Resolved in full.",
                        "evidence_ids": evidence_ids,
                    }
                ],
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": evidence_ids,
                        "note": "Full observation.",
                        "pending_questions": [],
                    }
                ],
                "starting_evidence_ids": evidence_ids,
                "evidence_ids": evidence_ids,
                "status": "in_progress",
            }
        ],
        "atom_drafts": [
            {
                "atom_id": "atom-main",
                "job_id": "job-main",
                "kind": "mechanism",
                "title": "Mechanism",
                "claim": "Complete claim.",
                "explanation": "Complete explanation.",
                "story_value": "Complete story value.",
                "source_refs": ["S01"],
                "quote_refs": evidence_ids[:2],
                "anchor_refs": ["anchor-main"],
                "evidence_ids": evidence_ids,
                "confidence": 0.8,
            }
        ],
        "research_gaps": [
            {
                "gap_id": "gap-main",
                "question": "What remains?",
                "source_refs": ["S01"],
                "evidence_ids": evidence_ids,
            }
        ],
        "risk_register": [
            {
                "risk": "Preserve this qualification.",
                "source_refs": ["S01"],
                "evidence_ids": evidence_ids,
            }
        ],
        "audit_repair_feedback": [
            {
                "target_step": "editorial_research",
                "description": "Repair this item.",
                "evidence_ids": evidence_ids,
            }
        ],
        "corpus_brief": {
            "summary": "Complete corpus summary.",
            "intent": {"user_prompt": "Complete user intent.", "language": "en"},
            "cautions": ["Complete caution."],
        },
    }
    source_map = SourceMap(
        source_cards=[
            SourceCard(
                source_id=source_id,
                title="Source",
                source_type="web",
                summary="Complete source summary.",
                representation_summary="Complete representation summary.",
                evidence_ids=evidence_ids,
            )
        ],
        source_assessments=[
            SourceAssessment(
                assessment_id="assessment-main",
                source_id=source_id,
                evidence_ids=evidence_ids,
            )
        ],
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="cluster-main",
                    label="Cluster",
                    source_ids=[source_id],
                    evidence_ids=evidence_ids,
                )
            ],
            source_relationships=[
                SourceRelationship(
                    source_id=source_id,
                    related_source_id=source_id,
                    relationship="updates",
                    evidence_ids=evidence_ids,
                )
            ],
        ),
    )

    async def load_triage(_db, _run_id):
        return triage_workspace

    async def load_research(_db, _run_id):
        return research_workspace

    monkeypatch.setattr(triage_workspace_api, "load_source_triage_workspace", load_triage)
    monkeypatch.setattr(research_workspace_api, "load_research_workspace", load_research)
    triage_tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=triage_workspace,
        inventory=source_map,
        mode="set",
        current_set_id="set-main",
    )
    research_tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=research_workspace,
        source_inventory=source_map,
        source_map=source_map,
        mode="job",
        current_job_id="job-main",
    )
    triage_page_tool = next(tool for tool in triage_tools if tool.name == "list_triage_source_sets")
    research_page_tool = next(tool for tool in research_tools if tool.name == "list_research_workspace_items")

    triage_set = await triage_page_tool.handler(
        {"section": "current_set", "cursor": "0", "limit": 1}
    )
    triage_observation = await triage_page_tool.handler(
        {"section": "observations", "cursor": "0", "limit": 1}
    )
    assert triage_set["items"][0]["evidence_ids"] == evidence_ids
    assert triage_observation["items"][0]["evidence_ids"] == evidence_ids
    assert triage_observation["items"][0]["evidence_omitted_count"] == 0

    expected_sections = {
        "jobs": "evidence_ids",
        "job_question_outcomes": "evidence_ids",
        "atom_drafts": "evidence_ids",
        "research_gaps": "evidence_ids",
        "workspace_risks": "evidence_ids",
        "audit_feedback": "evidence_ids",
        "observations": "evidence_ids",
        "source_map_cards": "evidence_ids",
        "source_map_assessments": "evidence_ids",
        "source_map_clusters": "evidence_ids",
        "source_map_relationships": "evidence_ids",
    }
    for section, field in expected_sections.items():
        page = await research_page_tool.handler(
            {"section": section, "cursor": "0", "limit": 1, "job_id": "job-main"}
        )
        assert page["accepted"] is True, (section, page)
        assert page["items"][0][field] == evidence_ids, section

    atom = await research_page_tool.handler(
        {"section": "atom_drafts", "cursor": "0", "limit": 1}
    )
    assert atom["items"][0]["quote_refs"] == evidence_ids[:2]
    assert atom["items"][0]["anchor_refs"] == ["anchor-main"]
    source_card = await research_page_tool.handler(
        {"section": "source_map_cards", "cursor": "0", "limit": 1}
    )
    assert source_card["items"][0]["representation_summary"] == "Complete representation summary."
    assert source_card["items"][0]["evidence_id_omitted_count"] == 0
    source_inventory_card = await research_page_tool.handler(
        {"section": "sources", "cursor": "0", "limit": 1}
    )
    assert source_inventory_card["items"][0]["summary"] == "Complete source summary."
    assert source_inventory_card["items"][0]["evidence_ids"] == evidence_ids
    assert "summary_preview" not in source_inventory_card["items"][0]
    for section, expected in (
        ("corpus_brief_summary", {"summary": "Complete corpus summary."}),
        ("corpus_brief_intent", {"user_prompt": "Complete user intent.", "language": "en"}),
        ("corpus_brief_cautions", {"caution": "Complete caution."}),
    ):
        page = await research_page_tool.handler({"section": section, "cursor": "0", "limit": 1})
        assert page["items"][0] == expected


@pytest.mark.asyncio
async def test_source_map_full_page_rejects_oversized_record_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    source_map = SourceMap(
        source_cards=[
            SourceCard(
                source_id=str(uuid.uuid4()),
                title="Source",
                source_type="web",
                summary="s" * 13_000,
            )
        ]
    )
    workspace: dict = {}
    before = deepcopy(source_map.model_dump(mode="json"))

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=source_map,
        source_map=source_map,
        mode="job",
        current_job_id="job-main",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")

    page = await page_tool.handler({"section": "source_map_cards", "cursor": "0", "limit": 1})

    assert page["accepted"] is False
    assert page["error"] == "workspace_item_exceeds_model_read_contract"
    assert "items" not in page
    assert source_map.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_workspace_handlers_reject_oversized_single_records_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as research_workspace_api
    from app.generation_v2 import source_triage_workspace as triage_workspace_api

    triage_workspace = {
        "coverage_questions": [
            {
                "question_id": "q-oversized",
                "question": "t" * 13_000,
                "kind": "context",
                "importance": "supporting",
                "rationale": "Still persisted in full.",
            }
        ]
    }
    research_workspace = {
        "current_job": {"job_id": "job-main"},
        "investigation_jobs": [
            {
                "job_id": "job-main",
                "questions": ["r" * 13_000],
                "observations": [],
                "status": "in_progress",
            }
        ],
    }
    triage_before = deepcopy(triage_workspace)
    research_before = deepcopy(research_workspace)

    async def load_triage(_db, _run_id):
        return triage_workspace

    async def load_research(_db, _run_id):
        return research_workspace

    monkeypatch.setattr(triage_workspace_api, "load_source_triage_workspace", load_triage)
    monkeypatch.setattr(research_workspace_api, "load_research_workspace", load_research)
    triage_tools = build_triage_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=triage_workspace,
        inventory=SourceMap(),
        mode="planner",
    )
    research_tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=research_workspace,
        source_inventory=SourceMap(),
        mode="job",
        current_job_id="job-main",
    )
    triage_page_tool = next(tool for tool in triage_tools if tool.name == "list_triage_source_sets")
    research_page_tool = next(tool for tool in research_tools if tool.name == "list_research_workspace_items")

    triage_page = await triage_page_tool.handler(
        {"section": "coverage_questions", "cursor": "0", "limit": 3}
    )
    research_page = await research_page_tool.handler(
        {"section": "current_job_questions", "cursor": "0", "limit": 3}
    )

    assert triage_page["accepted"] is False
    assert triage_page["error"] == "triage_workspace_item_exceeds_model_read_contract"
    assert "items" not in triage_page
    assert research_page["accepted"] is False
    assert research_page["error"] == "workspace_item_exceeds_model_read_contract"
    assert "items" not in research_page
    assert triage_workspace == triage_before
    assert research_workspace == research_before


def _collect_part_pages(fetch_page) -> list[dict]:
    items: list[dict] = []
    cursor = "0"
    while True:
        page = fetch_page(cursor)
        assert page["accepted"] is True
        assert len(json.dumps(page, ensure_ascii=False, default=str)) <= MODEL_TOOL_OUTPUT_MAX_CHARS
        items.extend(page["items"])
        cursor = page["page"]["next_cursor"]
        if not cursor:
            return items


def test_composite_preview_metadata_separates_preview_and_full_page_cursors() -> None:
    brief_page = corpus_brief_preview_page(30, 20, full_page_unit="record_part")
    assert brief_page["next_cursor"] == "20"
    assert brief_page["cursor_unit"] == "preview_record"
    assert brief_page["full_page_start_cursor"] == "0"
    assert brief_page["full_page_unit"] == "record_part"

    job_payload = current_job_prompt_payload(
        {
            "coverage_requirements": [
                {"question_id": "q", "requirement_id": f"r-{index}"}
                for index in range(13)
            ],
            "source_findings": [
                {"finding_id": f"finding-{index}"} for index in range(13)
            ],
        }
    )
    for section in ("coverage_requirements", "source_findings"):
        page = job_payload["pages"][section]
        assert page["next_cursor"] == "12"
        assert page["cursor_unit"] == "job_preview_record"
        assert page["full_page_start_cursor"] == "0"
        assert page["full_page_unit"] == "record_part"


def test_valid_large_question_is_fully_readable_as_stable_parts() -> None:
    requirements = [
        {
            "requirement_id": f"r-{index}",
            "description": f"description-{index}:" + "d" * 780,
            "rationale": f"rationale-{index}:" + "r" * 980,
        }
        for index in range(8)
    ]
    question = {
        "question_id": "q-large",
        "question": "Whole question",
        "kind": "mechanism",
        "importance": "critical",
        "rationale": "Whole rationale",
        "answer_requirements": requirements,
    }
    triage_workspace = {
        "coverage_questions": [question],
        "coverage_outcomes": [],
        "coverage_syntheses": [],
        "source_assessments": [],
        "triage_semantic_revision": 0,
    }
    research_workspace = {"corpus_brief": {"coverage_questions": [question]}}

    triage_parts = _collect_part_pages(
        lambda cursor: _full_canonical_item_page(
            triage_workspace,
            section="coverage_questions",
            cursor=int(cursor),
            limit=20,
        )
    )
    research_parts = _collect_part_pages(
        lambda cursor: _full_corpus_brief_page(
            research_workspace,
            SourceMap(),
            section="corpus_brief_questions",
            cursor=int(cursor),
            limit=20,
            include_evidence_ids=True,
        )
    )
    packet_parts = _collect_part_pages(
        lambda cursor: _coverage_question_packet_page(
            triage_workspace,
            cursor=int(cursor),
            limit=20,
        )
    )

    for parts in (triage_parts, research_parts):
        assert {item["owner_ref"] for item in parts} == {
            "coverage_question:q-large"
        }
        restored = [
            requirement
            for item in parts
            if item["part_kind"] == "answer_requirements"
            for requirement in item["items"]
        ]
        assert restored == requirements
        assert next(item for item in parts if item["part_kind"] == "core")["question"] == (
            "Whole question"
        )
    packet_question_parts = [item["question"] for item in packet_parts if item["part_kind"] == "question"]
    assert [
        requirement
        for item in packet_question_parts
        if item["part_kind"] == "answer_requirements"
        for requirement in item["items"]
    ] == requirements


def test_valid_large_finding_is_fully_readable_as_stable_parts() -> None:
    qualifiers = [f"qualifier-{index}:" + "q" * 780 for index in range(16)]
    limitations = [f"limitation-{index}:" + "l" * 780 for index in range(12)]
    finding = {
        "finding_id": "finding:S01:large",
        "set_id": "set-main",
        "source_ref": "S01",
        "source_id": "source-1",
        "statement": "A whole supported statement.",
        "evidence_ids": ["evidence-1"],
        "scope": {
            "actor": "Actor",
            "period": "Period",
            "geography": "Place",
            "qualifiers": qualifiers,
        },
        "limitations": limitations,
        "question_links": [],
        "source_relations": [],
        "semantic_revision": 3,
    }
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id="source-1",
                title="Source",
                source_type="web",
                summary="Source summary",
            )
        ]
    )
    triage_workspace = {"source_findings": [finding]}
    research_workspace = {"corpus_brief": {"source_findings": [finding]}}

    triage_parts = _collect_part_pages(
        lambda cursor: _full_canonical_item_page(
            triage_workspace,
            section="source_findings",
            cursor=int(cursor),
            limit=20,
        )
    )
    research_parts = _collect_part_pages(
        lambda cursor: _full_corpus_brief_page(
            research_workspace,
            inventory,
            section="corpus_brief_findings",
            cursor=int(cursor),
            limit=20,
            include_evidence_ids=True,
        )
    )

    for parts in (triage_parts, research_parts):
        assert {item["owner_ref"] for item in parts} == {"finding:S01:large"}
        assert [
            value
            for item in parts
            if item["part_kind"] == "finding_scope_qualifiers"
            for value in item["items"]
        ] == qualifiers
        assert [
            value
            for item in parts
            if item["part_kind"] == "finding_limitations"
            for value in item["items"]
        ] == limitations
        assert next(item for item in parts if item["part_kind"] == "core")["statement"] == (
            finding["statement"]
        )


def test_valid_large_outcome_requirement_results_are_fully_readable() -> None:
    requirement_results = [
        {
            "requirement_id": f"r-{index}",
            "status": "covered",
            "finding_ids": [f"finding-{index}"],
            "summary": f"summary-{index}:" + "s" * 1_560,
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.8,
            "next_action": "research",
        }
        for index in range(8)
    ]
    outcome = {
        "outcome_id": "outcome:q-large",
        "question_id": "q-large",
        "status": "covered",
        "source_ids": [],
        "evidence_ids": [f"evidence-{index}" for index in range(8)],
        "finding_ids": [f"finding-{index}" for index in range(8)],
        "requirement_results": requirement_results,
        "orientation_summary": "Whole orientation summary.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
        "next_action": "research",
    }
    triage_workspace = {"coverage_outcomes": [{**outcome, "set_id": "set-main"}]}
    research_workspace = {"corpus_brief": {"coverage_outcomes": [outcome]}}

    triage_parts = _collect_part_pages(
        lambda cursor: _full_canonical_item_page(
            triage_workspace,
            section="coverage_outcomes",
            cursor=int(cursor),
            limit=20,
        )
    )
    research_parts = _collect_part_pages(
        lambda cursor: _full_corpus_brief_page(
            research_workspace,
            SourceMap(),
            section="corpus_brief_outcomes",
            cursor=int(cursor),
            limit=20,
            include_evidence_ids=True,
        )
    )

    for parts in (triage_parts, research_parts):
        restored_results = [
            result
            for item in parts
            if item["part_kind"] == "outcome_requirement_results"
            for result in item["items"]
        ]
        assert [item["summary"] for item in restored_results] == [
            item["summary"] for item in requirement_results
        ]
        restored_finding_ids = {
            item["requirement_id"]: [str(value) for value in item["items"]]
            for item in parts
            if item["part_kind"] == "outcome_requirement_result_finding_ids"
        }
        assert restored_finding_ids == {
            item["requirement_id"]: item["finding_ids"] for item in requirement_results
        }
