from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_triage_workspace import build_workspace_tools
from app.generation_v2.source_triage_workspace_prompting import source_set_prompt_payload
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.tools import evidence_source_map_write_actions as write_actions
from app.generation_v2.tools.evidence_refs import build_source_ref_context


class _CapsuleEvidenceToolset:
    async def _resolve_source_version(self, **_kwargs):
        return None


def _inventory(count: int = 2) -> tuple[SourceMap, list[str]]:
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id=str(uuid.uuid4()),
                title=f"Source {index}",
                source_type="web",
                summary=f"Neutral catalog preview {index}.",
                coverage_status="ready",
            )
            for index in range(1, count + 1)
        ]
    )
    refs = list(build_source_ref_context(inventory).source_id_by_ref)
    return inventory, refs


def _question(
    question_id: str,
    *,
    importance: str = "critical",
    parent_question_id: str | None = None,
) -> dict:
    return {
        "question_id": question_id,
        "question": f"Question {question_id}?",
        "kind": "cause",
        "importance": importance,
        "rationale": "Serves the requested explanation.",
        "parent_question_id": parent_question_id,
        "origin": "planned",
        "answer_requirements": [
            {
                "requirement_id": f"{question_id}:r_core",
                "description": "Resolve the substantive answer to this question.",
                "rationale": "This is the semantic part required for a complete answer.",
            }
        ],
    }


def _source_set(
    set_id: str,
    *,
    set_kind: str,
    source_refs: list[str],
    question_ids: list[str],
    status: str = "planned",
) -> dict:
    planned_question_ids = list(question_ids) if set_kind == "coverage" else []
    return {
        "set_id": set_id,
        "set_kind": set_kind,
        "label": f"Set {set_id}",
        "purpose": "Read the assigned sources.",
        "source_refs": source_refs,
        "question_ids": list(question_ids),
        "question_assignments": [
            {
                "question_id": question_id,
                "requirement_ids": [f"{question_id}:r_core"],
            }
            for question_id in planned_question_ids
        ],
        "planned_question_ids": planned_question_ids,
        "cross_question_ids": [
            question_id for question_id in question_ids if question_id not in planned_question_ids
        ],
        "status": status,
        "rationale": "A bounded reading assignment.",
        "priority": "normal",
        "findings_summary": "",
        "evidence_ids": [],
        "written_actions": [],
        "observations": [],
        "deferred_reason": "",
        "repair": False,
    }


def _workspace(source_refs: list[str]) -> dict:
    return {
        "status": "orientation",
        "source_order": list(source_refs),
        "source_capsules": {},
        "planner_inspected_source_refs": [],
        "coverage_questions": [],
        "source_sets": [],
        "source_assessments": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [],
        "processed_sets": [],
        "deferred_sources": [],
        "mapped_sources": [],
    }


def _install_workspace_store(monkeypatch: pytest.MonkeyPatch, workspace: dict) -> dict:
    state = {"workspace": workspace}

    async def _load(_db, _run_id):
        return state["workspace"]

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        state["workspace"] = workspace
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace, status=status)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", _load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", _save)
    return state


def _tools(
    *,
    inventory: SourceMap,
    workspace: dict,
    mode: str,
    current_set_id: str | None = None,
    evidence_toolset=None,
):
    return {
        tool.name: tool
        for tool in build_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=SimpleNamespace(id=uuid.uuid4(), config={}),  # type: ignore[arg-type]
            workspace=workspace,
            inventory=inventory,
            evidence_toolset=evidence_toolset,
            mode=mode,
            current_set_id=current_set_id,
        )
    }


@pytest.mark.asyncio
async def test_orientation_route_requires_explicit_single_source_capsule_reads_and_accepts_many_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(2)
    state = _install_workspace_store(monkeypatch, _workspace(refs))
    tools = _tools(
        inventory=inventory,
        workspace=state["workspace"],
        mode="planner",
        evidence_toolset=_CapsuleEvidenceToolset(),
    )

    bulk_page = await tools["list_source_capsules"].handler(
        {"limit": 10, "cursor": "", "source_refs": []}
    )
    rejected = await tools["route_sources_for_initial_reading"].handler(
        {"source_refs": refs}
    )

    assert bulk_page["accepted"] is True
    assert rejected["accepted"] is False
    assert rejected["uninspected_source_refs"] == refs
    assert state["workspace"]["source_sets"] == []

    for source_ref in refs:
        inspected = await tools["list_source_capsules"].handler(
            {"limit": 1, "cursor": "", "source_refs": [source_ref]}
        )
        assert inspected["accepted"] is True
        assert inspected["requested_source_refs"] == [source_ref]

    routed = await tools["route_sources_for_initial_reading"].handler(
        {"source_refs": refs}
    )

    assert routed["accepted"] is True
    assert len(state["workspace"]["source_sets"]) == 1
    orientation_set = state["workspace"]["source_sets"][0]
    assert orientation_set["set_kind"] == "orientation"
    assert orientation_set["source_refs"] == refs
    assert orientation_set["question_ids"] == []
    assert orientation_set["planned_question_ids"] == []


@pytest.mark.asyncio
async def test_regular_source_set_path_always_creates_coverage_and_still_requires_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(1)
    workspace = _workspace(refs)
    workspace["coverage_questions"] = [_question("q_root")]
    state = _install_workspace_store(monkeypatch, workspace)
    tools = _tools(inventory=inventory, workspace=workspace, mode="planner")
    upsert = tools["upsert_triage_source_set"]
    common = {
        "set_id": "coverage_set",
        "label": "Coverage set",
        "purpose": "Orient the central question.",
        "source_refs": refs,
        "rationale": "The catalog gives a topical basis.",
        "priority": "normal",
    }

    rejected = await upsert.handler({**common, "question_ids": []})
    accepted = await upsert.handler({**common, "question_ids": ["q_root"]})

    assert rejected["accepted"] is False
    assert accepted["accepted"] is True
    assert len(state["workspace"]["source_sets"]) == 1
    assert state["workspace"]["source_sets"][0]["set_kind"] == "coverage"
    assert "set_kind" not in upsert.parameters["properties"]


@pytest.mark.asyncio
async def test_finalize_accepts_questionless_orientation_set_when_coverage_questions_are_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(2)
    workspace = _workspace(refs)
    workspace["planner_inspected_source_refs"] = [refs[0]]
    workspace["coverage_questions"] = [
        _question("q_root"),
        _question("q_detail", importance="supporting", parent_question_id="q_root"),
    ]
    workspace["source_sets"] = [
        _source_set(
            "orientation_set",
            set_kind="orientation",
            source_refs=[refs[0]],
            question_ids=[],
        ),
        _source_set(
            "coverage_set",
            set_kind="coverage",
            source_refs=[refs[1]],
            question_ids=["q_root", "q_detail"],
        ),
    ]
    state = _install_workspace_store(monkeypatch, workspace)
    finalize = _tools(inventory=inventory, workspace=workspace, mode="planner")[
        "finalize_triage_workspace_plan"
    ]

    result = await finalize.handler(
        {"summary": "Every source is assigned once.", "central_question_id": "q_root"}
    )

    assert result["accepted"] is True
    assert state["workspace"]["status"] == "planned"
    assert state["workspace"]["central_question_id"] == "q_root"


@pytest.mark.asyncio
async def test_finalize_requires_every_real_question_in_a_coverage_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(2)
    workspace = _workspace(refs)
    workspace["planner_inspected_source_refs"] = [refs[0]]
    workspace["coverage_questions"] = [
        _question("q_root"),
        _question("q_unassigned", importance="supporting", parent_question_id="q_root"),
    ]
    workspace["source_sets"] = [
        _source_set(
            "orientation_set",
            set_kind="orientation",
            source_refs=[refs[0]],
            question_ids=[],
        ),
        _source_set(
            "coverage_set",
            set_kind="coverage",
            source_refs=[refs[1]],
            question_ids=["q_root"],
        ),
    ]
    _install_workspace_store(monkeypatch, workspace)
    finalize = _tools(inventory=inventory, workspace=workspace, mode="planner")[
        "finalize_triage_workspace_plan"
    ]

    result = await finalize.handler(
        {"summary": "Incomplete plan.", "central_question_id": "q_root"}
    )

    assert result["accepted"] is False
    assert result["unassigned_question_ids"] == ["q_unassigned"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "central_question_id",
    ["", "q_missing", "q_child", "q_supporting"],
)
async def test_finalize_requires_central_question_to_be_existing_root_and_critical(
    monkeypatch: pytest.MonkeyPatch,
    central_question_id: str,
) -> None:
    inventory, refs = _inventory(1)
    workspace = _workspace(refs)
    workspace["coverage_questions"] = [
        _question("q_root"),
        _question("q_child", parent_question_id="q_root"),
        _question("q_supporting", importance="supporting"),
    ]
    workspace["source_sets"] = [
        _source_set(
            "coverage_set",
            set_kind="coverage",
            source_refs=refs,
            question_ids=["q_root", "q_child", "q_supporting"],
        )
    ]
    _install_workspace_store(monkeypatch, workspace)
    finalize = _tools(inventory=inventory, workspace=workspace, mode="planner")[
        "finalize_triage_workspace_plan"
    ]

    result = await finalize.handler(
        {"summary": "Plan.", "central_question_id": central_question_id}
    )

    assert result["accepted"] is False


@pytest.mark.asyncio
async def test_question_update_cannot_create_a_parent_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(1)
    workspace = _workspace(refs)
    workspace["coverage_questions"] = [
        _question("q_root"),
        _question("q_child", importance="supporting", parent_question_id="q_root"),
    ]
    state = _install_workspace_store(monkeypatch, workspace)
    upsert = _tools(inventory=inventory, workspace=workspace, mode="planner")[
        "upsert_coverage_question"
    ]

    result = await upsert.handler(
        {
            "question_id": "q_root",
            "question": "Question q_root?",
            "kind": "cause",
            "importance": "critical",
            "rationale": "Serves the requested explanation.",
            "parent_question_id": "q_child",
            "answer_requirements": [
                {
                    "requirement_id": "q_root:r_core",
                    "description": "Resolve the substantive answer to this question.",
                    "rationale": "Required for a complete answer.",
                }
            ],
        }
    )

    assert result["accepted"] is False
    assert "cycle" in result["error"]
    assert state["workspace"]["coverage_questions"][0]["parent_question_id"] is None


def test_source_set_prompt_payload_marks_orientation_work_without_inventing_questions() -> None:
    inventory, refs = _inventory(1)
    del inventory
    workspace = _workspace(refs)
    workspace["source_sets"] = [
        _source_set(
            "orientation_set",
            set_kind="orientation",
            source_refs=refs,
            question_ids=[],
            status="in_progress",
        )
    ]

    source_set = source_set_prompt_payload(workspace, "orientation_set")["set"]

    assert source_set["set_kind"] == "orientation"
    assert source_set["question_ids"] == []
    assert source_set["required_question_ids"] == []
    assert source_set["missing_outcome_question_ids"] == []
    assert "independently" in source_set["execution_goal"]


def _orientation_execution_workspace(refs: list[str]) -> dict:
    workspace = _workspace(refs)
    workspace["status"] = "planned"
    workspace["coverage_questions"] = [_question("q_root")]
    workspace["source_sets"] = [
        _source_set(
            "orientation_set",
            set_kind="orientation",
            source_refs=[refs[0]],
            question_ids=[],
            status="in_progress",
        ),
        _source_set(
            "coverage_set",
            set_kind="coverage",
            source_refs=[refs[1]],
            question_ids=["q_root"],
        ),
    ]
    workspace["current_set"] = {"set_id": "orientation_set", "source_refs": [refs[0]]}
    return workspace


def _source_analysis_args(source_ref: str, *, question_ids: list[str]) -> dict:
    evidence_id = str(uuid.uuid4())
    findings = [
        {
            "finding_id": "orientation_finding",
            "statement": "The inspected source establishes a substantive answer component.",
            "evidence_ids": [evidence_id],
            "scope": {
                "actor": "The actor named in the source",
                "period": "The period covered by the source",
                "geography": "The geography covered by the source",
                "qualifiers": [],
            },
            "limitations": [],
            "question_links": [
                {
                    "question_id": question_id,
                    "requirement_ids": [f"{question_id}:r_core"],
                    "rationale": "The saved evidence directly supports this answer requirement.",
                }
                for question_id in question_ids
            ],
            "source_relations": [],
        }
    ] if question_ids else []
    return {
        "set_id": "orientation_set",
        "source_ref": source_ref,
        "processing_status": "resolved",
        "representation_status": "readable",
        "relevance": "supporting",
        "authority": "expert",
        "uniqueness": "unique",
        "disposition": "supporting",
        "representation_reason": "The available representation was read.",
        "relevance_reason": "The source may inform downstream research.",
        "authority_reason": "The source gives an attributable expert account.",
        "uniqueness_reason": "No duplication was established.",
        "disposition_reason": "Keep as a possible supporting source.",
        "editorial_functions": [],
        "evidence_ids": [evidence_id],
        "limitations": [],
        "confidence": 0.7,
        "no_findings_reason": (
            "The inspected source yielded no substantive finding for any current question."
            if not findings
            else ""
        ),
        "findings": findings,
    }


def _finish_args() -> dict:
    return {
        "set_id": "orientation_set",
        "status": "mapped",
        "summary": "Initial reading completed.",
        "deferred_reason": "",
        "deferred_sources": [],
    }


async def _allow_evidence(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_orientation_set_finishes_after_assessments_without_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(2)
    workspace = _orientation_execution_workspace(refs)
    state = _install_workspace_store(monkeypatch, workspace)
    monkeypatch.setattr(
        SourceTriageWorkspaceToolHandlers,
        "_validate_set_evidence_ids",
        _allow_evidence,
    )
    monkeypatch.setattr(
        SourceTriageWorkspaceToolHandlers,
        "_select_validated_evidence_ids",
        _allow_evidence,
    )
    tools = _tools(
        inventory=inventory,
        workspace=workspace,
        mode="set",
        current_set_id="orientation_set",
    )

    assessment = await tools["save_source_analysis"].handler(
        _source_analysis_args(refs[0], question_ids=[])
    )
    finished = await tools["finish_triage_source_set"].handler(_finish_args())

    assert assessment["accepted"] is True
    assert finished["accepted"] is True
    orientation_set = next(
        item for item in state["workspace"]["source_sets"] if item["set_id"] == "orientation_set"
    )
    assert orientation_set["question_ids"] == []
    assert orientation_set["outcome_question_ids"] == []


@pytest.mark.asyncio
async def test_orientation_cross_question_requires_an_outcome_before_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(2)
    workspace = _orientation_execution_workspace(refs)
    state = _install_workspace_store(monkeypatch, workspace)
    monkeypatch.setattr(
        SourceTriageWorkspaceToolHandlers,
        "_validate_set_evidence_ids",
        _allow_evidence,
    )
    monkeypatch.setattr(
        SourceTriageWorkspaceToolHandlers,
        "_select_validated_evidence_ids",
        _allow_evidence,
    )
    tools = _tools(
        inventory=inventory,
        workspace=workspace,
        mode="set",
        current_set_id="orientation_set",
    )

    assessment = await tools["save_source_analysis"].handler(
        _source_analysis_args(refs[0], question_ids=["q_root"])
    )
    rejected_finish = await tools["finish_triage_source_set"].handler(_finish_args())

    orientation_set = next(
        item for item in state["workspace"]["source_sets"] if item["set_id"] == "orientation_set"
    )
    assert assessment["accepted"] is True
    assert orientation_set["planned_question_ids"] == []
    assert orientation_set["cross_question_ids"] == ["q_root"]
    assert rejected_finish["accepted"] is False
    assert rejected_finish["missing_question_ids"] == ["q_root"]

    outcome = await tools["upsert_coverage_outcome"].handler(
        {
            "set_id": "orientation_set",
            "question_id": "q_root",
            "status": "covered",
            "finding_ids": assessment["finding_ids"],
            "requirement_results": [
                {
                    "requirement_id": "q_root:r_core",
                    "status": "covered",
                    "finding_ids": assessment["finding_ids"],
                    "summary": "The finding establishes the answer component.",
                    "missing_detail": "",
                    "conflict_summary": "",
                    "deferred_reason": "",
                    "confidence": 0.7,
                }
            ],
            "orientation_summary": "The finding establishes the answer component.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.7,
        }
    )
    accepted_finish = await tools["finish_triage_source_set"].handler(_finish_args())

    assert outcome["accepted"] is True
    assert accepted_finish["accepted"] is True


@pytest.mark.asyncio
async def test_orientation_source_analyses_are_atomic_and_source_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, refs = _inventory(3)
    workspace = _workspace(refs)
    workspace["status"] = "planned"
    workspace["coverage_questions"] = [_question("q_root")]
    workspace["source_sets"] = [
        _source_set(
            "orientation_set",
            set_kind="orientation",
            source_refs=refs[:2],
            question_ids=[],
            status="in_progress",
        ),
        _source_set(
            "coverage_set",
            set_kind="coverage",
            source_refs=[refs[2]],
            question_ids=["q_root"],
        ),
    ]
    workspace["current_set"] = {"set_id": "orientation_set", "source_refs": refs[:2]}
    state = _install_workspace_store(monkeypatch, workspace)
    monkeypatch.setattr(
        SourceTriageWorkspaceToolHandlers,
        "_validate_set_evidence_ids",
        _allow_evidence,
    )
    monkeypatch.setattr(
        SourceTriageWorkspaceToolHandlers,
        "_select_validated_evidence_ids",
        _allow_evidence,
    )
    tools = _tools(
        inventory=inventory,
        workspace=workspace,
        mode="set",
        current_set_id="orientation_set",
    )

    assert "record_triage_observation" not in tools
    assert "source_ref" in tools["save_source_analysis"].parameters["required"]
    assert "source_refs" not in tools["save_source_analysis"].parameters["properties"]

    first = await tools["save_source_analysis"].handler(
        _source_analysis_args(refs[0], question_ids=[])
    )
    second = await tools["save_source_analysis"].handler(
        _source_analysis_args(refs[1], question_ids=[])
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert {item["source_ref"] for item in state["workspace"]["source_assessments"]} == set(refs[:2])


@pytest.mark.asyncio
async def test_orientation_gaps_must_remain_source_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4()
    source_ids = {"S01": str(uuid.uuid4()), "S02": str(uuid.uuid4())}
    workspace = {
        "source_sets": [
            {
                "set_id": "orientation_set",
                "set_kind": "orientation",
                "source_refs": ["S01", "S02"],
                "question_ids": [],
            }
        ]
    }

    async def load_workspace(_db, _run_id):
        return workspace

    async def no_workspace_write(*_args, **_kwargs):
        return None

    async def no_scope_error(_refs):
        return None

    async def source_ids_from_refs(refs):
        return [source_ids[ref] for ref in refs], []

    async def source_ref_for_id(source_id):
        return next(ref for ref, value in source_ids.items() if value == str(source_id))

    async def select_evidence(_ids, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(write_actions, "mark_workspace_write_action", no_workspace_write)
    owner = SimpleNamespace(
        db=None,
        run=SimpleNamespace(id=run_id),
        triage_current_set_id="orientation_set",
        _triage_write_scope_error_for_refs=no_scope_error,
        _source_ids_from_refs_or_ids=source_ids_from_refs,
        _source_ref_for_id=source_ref_for_id,
        _select_evidence_ids_for_write=select_evidence,
    )
    common = {
        "kind": "representation",
        "description": "The requested source content was not obtained.",
        "question_ids": [],
        "evidence_ids": [],
    }

    rejected = await write_actions.record_gap(
        owner,
        {**common, "source_refs": ["S01", "S02"]},
    )
    accepted = await write_actions.record_gap(
        owner,
        {**common, "source_refs": ["S01"]},
    )

    assert rejected["accepted"] is False
    assert rejected["error"] == "orientation_gap_requires_one_source"
    assert accepted["accepted"] is True
    assert accepted["source_count"] == 1
