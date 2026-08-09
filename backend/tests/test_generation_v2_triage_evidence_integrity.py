from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.research_source_map_prompting import source_map_research_page
from app.generation_v2.schemas import (
    SourceAssessment,
    SourceCard,
    SourceCluster,
    SourceGapRecord,
    SourceMap,
    SourceRelationship,
    SourceTriageReport,
)
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.source_triage_set_state_views import question_workbench_signature
from app.generation_v2.source_triage_workspace_tracking import (
    _successful_commit_trace,
    _successful_read_trace,
)
from app.generation_v2.tools import evidence_source_map_write_actions as write_actions
from app.generation_v2.tools.evidence_source_map_actions import build_source_map_from_write_actions
from app.generation_v2.tools.evidence_source_map_gate import validate_source_map_gate
from app.generation_v2.tools.evidence_source_map_ids import source_map_record_fingerprint
from app.generation_v2.tools.evidence_tool_write_specs import evidence_write_tools
from app.generation_v2.tools.evidence_tool_source_read_specs import evidence_source_read_tools
from app.models.generation_v2 import EvidenceStrength


class _EvidenceToolset:
    def __init__(self, evidence: dict[str, SimpleNamespace], refs: dict[str, str]) -> None:
        self.evidence = evidence
        self.refs = refs
        self.selection_calls: list[dict] = []

    async def _evidence_in_run(self, evidence_id: str):
        return self.evidence.get(evidence_id)

    async def _source_ref_for_id(self, source_id) -> str:
        return self.refs[str(source_id)]

    async def _select_evidence_ids_for_write(self, evidence_ids: list[str], **kwargs):
        self.selection_calls.append({"evidence_ids": list(evidence_ids), **kwargs})
        for evidence_id in evidence_ids:
            item = self.evidence[evidence_id]
            item.metadata_ = {**(item.metadata_ or {}), "usage_status": "selected"}
        return []


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self):
        return self.values


class _WriteActionDb:
    def __init__(self, traces: list[object], evidence: list[object]) -> None:
        self.responses = [_ScalarResult(traces), _ScalarResult(evidence)]

    async def execute(self, _query):
        return self.responses.pop(0)


class _ActionLineageDb:
    def __init__(self, responses: list[list[object]]) -> None:
        self.responses = [_ScalarResult(items) for items in responses]
        self.added: list[object] = []

    async def execute(self, _query):
        return self.responses.pop(0)

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None


def test_triage_commit_guard_counts_only_successful_reads() -> None:
    assert _successful_read_trace(SimpleNamespace(output_json={"found": True})) is True
    assert _successful_read_trace(SimpleNamespace(output_json={"accepted": True, "results": []})) is False
    assert _successful_read_trace(SimpleNamespace(output_json={"found": False, "error": "invalid_window_ref"})) is False
    assert _successful_read_trace(SimpleNamespace(output_json={"accepted": False})) is False
    assert _successful_read_trace(SimpleNamespace(output_json={"error": "tool_execution_failed"})) is False
    assert _successful_commit_trace(SimpleNamespace(output_json={"accepted": True})) is True
    assert _successful_commit_trace(SimpleNamespace(output_json={"accepted": False})) is False
    assert _successful_commit_trace(SimpleNamespace(output_json={"error": "tool_execution_failed"})) is False
    assert _successful_commit_trace(
        SimpleNamespace(output_json={"accepted": True, "_runtime_error": {"type": "RuntimeError"}})
    ) is False


@pytest.mark.asyncio
async def test_new_attempt_carries_full_source_map_actions_and_remaps_evidence() -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    old_evidence_id = str(uuid.uuid4())
    new_evidence_id = str(uuid.uuid4())
    invalid_evidence_id = str(uuid.uuid4())
    traces = [
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="record_source_relationship",
            input_json={
                "source_ref": "S01",
                "related_source_ref": "S02",
                "relationship": "background_for",
                "evidence_ids": [old_evidence_id],
                "rationale": "Preserve the full relationship rationale.",
            },
            output_json={"accepted": True},
            created_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="record_source_cluster",
            input_json={
                "cluster_id": "cluster-history",
                "label": "Historical context",
                "source_refs": ["S01", "S02"],
                "primary_source_refs": ["S01"],
                "secondary_source_refs": ["S02"],
                "duplicate_source_refs": [],
                "evidence_ids": [old_evidence_id],
                "rationale": "Preserve the full cluster rationale.",
            },
            output_json={"accepted": True},
            created_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="record_source_relationship",
            input_json={
                "source_ref": "S02",
                "related_source_ref": "S01",
                "relationship": "updates",
                "evidence_ids": [old_evidence_id],
                "rationale": "This relationship was later removed.",
            },
            output_json={"accepted": True},
            created_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="resolve_source_map_repair",
            input_json={
                "target_kind": "relationship",
                "source_id": source_2,
                "related_source_id": source_1,
                "relationship": "updates",
                "target_fingerprint": "fingerprint-before-evidence-remap",
            },
            output_json={"accepted": True},
            created_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="create_quote_card",
            input_json={"evidence_id": invalid_evidence_id, "quote": "Unavailable quote."},
            output_json={"accepted": True},
            created_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="finalize_source_map",
            input_json={"ready": True, "summary": "Old finalization."},
            output_json={"accepted": True},
            created_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_name="record_gap",
            input_json={"description": "Rejected gap."},
            output_json={"accepted": False},
            created_at=None,
        ),
    ]
    db = _ActionLineageDb([traces, []])
    run_id = uuid.uuid4()

    carried_count = await workspace_api._carry_source_map_write_actions_to_current_attempt(
        db,  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id),
        run_view=SimpleNamespace(revision=1, attempt=2),
        source_attempt=1,
        evidence_id_map={old_evidence_id: new_evidence_id},
    )

    assert carried_count == 4
    assert len(db.added) == 4
    relationship = next(
        item
        for item in db.added
        if item.tool_name == "record_source_relationship"
        and item.input_json["relationship"] == "background_for"
    )
    carried_by_tool = {item.tool_name: item for item in db.added}
    cluster = carried_by_tool["record_source_cluster"]
    resolution = carried_by_tool["resolve_source_map_repair"]
    assert relationship.input_json["evidence_ids"] == [new_evidence_id]
    assert relationship.input_json["rationale"] == "Preserve the full relationship rationale."
    assert cluster.input_json["label"] == "Historical context"
    assert cluster.input_json["rationale"] == "Preserve the full cluster rationale."
    assert resolution.input_json["target_fingerprint"] == ""
    assert all(item.attempt == 2 for item in db.added)
    assert all(item.output_json["_attempt_lineage"]["from_attempt"] == 1 for item in db.added)

    source_map = await build_source_map_from_write_actions(
        _WriteActionDb(
            db.added,
            [_evidence(new_evidence_id, source_1)],
        ),  # type: ignore[arg-type]
        generation_run_id=run_id,
        inventory=SourceMap(
            source_cards=[
                _card(source_1, "First"),
                _card(source_2, "Second"),
            ]
        ),
        run_view=SimpleNamespace(revision=1, attempt=2),
    )

    assert source_map.source_triage.source_relationships[0].rationale == (
        "Preserve the full relationship rationale."
    )
    assert len(source_map.source_triage.source_relationships) == 1
    assert source_map.source_triage.source_clusters[0].label == "Historical context"

    replacement_evidence_id = str(uuid.uuid4())
    db.responses = [_ScalarResult(traces), _ScalarResult(db.added)]
    repeated_count = await workspace_api._carry_source_map_write_actions_to_current_attempt(
        db,  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id),
        run_view=SimpleNamespace(revision=1, attempt=2),
        source_attempt=1,
        evidence_id_map={old_evidence_id: replacement_evidence_id},
    )

    assert repeated_count == 4
    assert len(db.added) == 4
    assert relationship.input_json["evidence_ids"] == [replacement_evidence_id]


def _card(source_id: str, title: str) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[str(uuid.uuid4())],
        title=title,
        source_type="web",
        summary=f"{title} summary",
    )


def _evidence(
    evidence_id: str,
    source_id: str,
    *,
    locator: str = "section 1",
    snippet: str = "A grounded passage from the source.",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID(evidence_id),
        source_id=uuid.UUID(source_id),
        source_version_id=uuid.uuid4(),
        snippet_text=snippet,
        locator_text=locator,
        retrieval_channel="local_chunk_search",
        evidence_strength=EvidenceStrength.EXACT,
        metadata_={"usage_status": "observed"},
        purpose="source_read",
    )


def _workspace(*, source_refs: list[str], question_ids: list[str] | None = None) -> dict:
    assigned_question_ids = question_ids or ["q1"]
    return {
        "current_set": {"set_id": "set_1"},
        "coverage_questions": [
            {"question_id": question_id, "question": f"Question {question_id}"}
            for question_id in assigned_question_ids
        ],
        "source_sets": [
            {
                "set_id": "set_1",
                "status": "in_progress",
                "source_refs": source_refs,
                "question_ids": assigned_question_ids,
                "planned_question_ids": assigned_question_ids,
                "cross_question_ids": [],
                "observations": [],
                "written_actions": [],
                "evidence_ids": [],
            }
        ],
        "source_assessments": [],
        "coverage_outcomes": [],
    }


def _assessment_args(evidence_ids: list[str]) -> dict:
    return {
        "set_id": "set_1",
        "source_ref": "S01",
        "processing_status": "resolved",
        "representation_status": "readable",
        "representation_reason": "The representation was read successfully.",
        "relevance": "core",
        "relevance_reason": "It addresses the assigned question.",
        "authority": "official",
        "authority_reason": "It is the responsible institution's publication.",
        "uniqueness": "unique",
        "uniqueness_reason": "It contains the original account.",
        "disposition": "anchor",
        "disposition_reason": "",
        "coverage_question_ids": ["q1"],
        "editorial_functions": ["mechanism"],
        "evidence_ids": evidence_ids,
        "limitations": [],
        "confidence": 0.8,
    }


def test_deferred_source_reason_explains_representation_failure_before_relevance() -> None:
    reason = SourceTriageWorkspaceToolHandlers._assessment_deferral_reason(
        {
            "representation_reason": "The available representation contains no readable source text.",
            "relevance_reason": "The source appears relevant to the assigned question.",
            "uniqueness_reason": "Its value could not be assessed.",
        }
    )

    assert reason == "The available representation contains no readable source text."


def _outcome_args(*, status: str, source_refs: list[str], evidence_ids: list[str]) -> dict:
    return {
        "set_id": "set_1",
        "question_id": "q1",
        "status": status,
        "source_refs": source_refs,
        "evidence_ids": evidence_ids,
        "orientation_summary": "The source partially answers the question.",
        "missing_detail": "One detail remains open." if status == "partial" else "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.7,
        "next_action": "research",
    }


def _patch_workspace(monkeypatch: pytest.MonkeyPatch, workspace: dict) -> dict[str, dict]:
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    return state


def _structured_workspace(*, source_refs: list[str], assigned_requirement_ids: list[str]) -> dict:
    workspace = _workspace(source_refs=source_refs)
    workspace["coverage_questions"] = [
        {
            "question_id": "q1",
            "question": "What happened and why?",
            "answer_requirements": [
                {"requirement_id": "q1:r1", "description": "Establish what happened.", "rationale": ""},
                {"requirement_id": "q1:r2", "description": "Establish why it happened.", "rationale": ""},
            ],
        }
    ]
    workspace["source_sets"][0]["question_assignments"] = [
        {"question_id": "q1", "requirement_ids": assigned_requirement_ids}
    ]
    workspace["source_findings"] = []
    workspace["triage_semantic_revision"] = 0
    return workspace


def _source_analysis_args(
    *,
    source_ref: str,
    evidence_ids: list[str],
    findings: list[dict],
    no_findings_reason: str = "",
) -> dict:
    args = _assessment_args(evidence_ids)
    args.pop("coverage_question_ids")
    return {
        **args,
        "source_ref": source_ref,
        "findings": findings,
        "no_findings_reason": no_findings_reason,
    }


def _finding_args(
    *, finding_id: str, evidence_id: str, requirement_ids: list[str]
) -> dict:
    return {
        "finding_id": finding_id,
        "statement": "The source establishes a bounded part of the answer.",
        "evidence_ids": [evidence_id],
        "scope": {"actor": "Named actor", "period": "Named period", "geography": "", "qualifiers": []},
        "limitations": [],
        "question_links": [
            {
                "question_id": "q1",
                "requirement_ids": requirement_ids,
                "rationale": "The passage directly supports these answer requirements.",
            }
        ],
        "source_relations": [],
    }


def _requirement_result(
    requirement_id: str,
    status: str,
    *,
    finding_ids: list[str] | None = None,
) -> dict:
    return {
        "requirement_id": requirement_id,
        "status": status,
        "finding_ids": finding_ids or [],
        "summary": "Established." if status in {"covered", "partial"} else "",
        "missing_detail": "Still absent." if status in {"partial", "missing"} else "",
        "conflict_summary": "Conflicting evidence." if status == "conflicted" else "",
        "deferred_reason": "The checked representation blocks evaluation." if status == "deferred" else "",
        "confidence": 0.8,
    }


@pytest.mark.asyncio
async def test_local_outcome_accepts_only_requirements_assigned_to_this_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _structured_workspace(source_refs=["S01"], assigned_requirement_ids=["q1:r1"])
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)}, {source_id: "S01"}
        ),
        current_set_id="set_1",
    )
    finding_id = "finding:S01:assigned-part"

    saved = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )
    outcome = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "covered",
            "requirement_results": [
                _requirement_result("q1:r1", "covered", finding_ids=[finding_id])
            ],
            "orientation_summary": "The assigned part is established.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.8,
        }
    )

    assert saved["accepted"] is True
    assert outcome["accepted"] is True
    assert state["workspace"]["coverage_outcomes"][0]["finding_ids"] == [finding_id]


@pytest.mark.asyncio
async def test_coverage_hint_accepts_only_current_source_question_and_material_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _structured_workspace(source_refs=["S01"], assigned_requirement_ids=["q1:r1"])
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)}, {source_id: "S01"}
        ),
        current_set_id="set_1",
    )
    finding_id = "finding:S01:hinted-part"

    saved = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )
    accepted = await handler.save_coverage_hint_tool(
        {
            "set_id": "set_1",
            "source_ref": "S01",
            "question_id": "q1",
            "requirement_ids": ["q1:r1"],
            "candidate_finding_ids": [finding_id],
            "candidate_evidence_ids": [evidence_id],
            "observed_gap": "",
            "conflict_signal": "",
            "notes": ["This source points research at the first requirement."],
        }
    )
    rejected = await handler.save_coverage_hint_tool(
        {
            "set_id": "set_1",
            "source_ref": "S01",
            "question_id": "q1",
            "requirement_ids": ["q1:unknown"],
            "candidate_finding_ids": [finding_id],
            "candidate_evidence_ids": [evidence_id],
            "observed_gap": "",
            "conflict_signal": "",
            "notes": ["Wrong requirement id."],
        }
    )

    assert saved["accepted"] is True
    assert accepted["accepted"] is True
    assert rejected["accepted"] is False
    assert rejected["error"] == "coverage_hint_requirement_ids_must_belong_to_question"
    assert state["workspace"]["coverage_hints"][0]["candidate_finding_ids"] == [finding_id]
    assert state["workspace"]["coverage_hints"][0]["candidate_evidence_ids"] == [evidence_id]
    assert state["workspace"]["coverage_outcomes"] == []


@pytest.mark.asyncio
async def test_changed_source_analysis_retires_old_coverage_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    old_evidence_id = str(uuid.uuid4())
    new_evidence_id = str(uuid.uuid4())
    workspace = _structured_workspace(source_refs=["S01"], assigned_requirement_ids=["q1:r1"])
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {
                old_evidence_id: _evidence(old_evidence_id, source_id),
                new_evidence_id: _evidence(new_evidence_id, source_id),
            },
            {source_id: "S01"},
        ),
        current_set_id="set_1",
    )
    old_finding_id = "finding:S01:old"
    new_finding_id = "finding:S01:new"

    first = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=old_finding_id,
                    evidence_id=old_evidence_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )
    hint = await handler.save_coverage_hint_tool(
        {
            "set_id": "set_1",
            "source_ref": "S01",
            "question_id": "q1",
            "requirement_ids": ["q1:r1"],
            "candidate_finding_ids": [old_finding_id],
            "candidate_evidence_ids": [old_evidence_id],
            "observed_gap": "",
            "conflict_signal": "",
            "notes": ["The old analysis exposed this lead."],
        }
    )
    replacement = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=new_finding_id,
                    evidence_id=new_evidence_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )

    assert first["accepted"] is True
    assert hint["accepted"] is True
    assert replacement["accepted"] is True
    assert replacement["retired_coverage_hint_ids"] == [hint["hint_id"]]
    assert state["workspace"]["coverage_hints"] == []
    assert state["workspace"]["retired_coverage_hints"][0]["hint_id"] == hint["hint_id"]
    assert state["workspace"]["retired_coverage_hints"][0]["retired_reason"] == (
        "source_analysis_changed"
    )


@pytest.mark.asyncio
async def test_later_source_finding_stales_and_then_refreshes_provisional_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a_id = str(uuid.uuid4())
    source_b_id = str(uuid.uuid4())
    evidence_a_id = str(uuid.uuid4())
    evidence_b_id = str(uuid.uuid4())
    workspace = _structured_workspace(
        source_refs=["S01", "S02"], assigned_requirement_ids=["q1:r1"]
    )
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[
                _card(source_a_id, "First"),
                _card(source_b_id, "Second"),
            ]
        ),
        evidence_toolset=_EvidenceToolset(
            {
                evidence_a_id: _evidence(evidence_a_id, source_a_id),
                evidence_b_id: _evidence(evidence_b_id, source_b_id),
            },
            {source_a_id: "S01", source_b_id: "S02"},
        ),
        current_set_id="set_1",
    )
    finding_a_id = "finding:S01:first"
    finding_b_id = "finding:S02:second"

    first_source = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=finding_a_id,
                    evidence_id=evidence_a_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )
    provisional = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "covered",
            "finding_ids": [finding_a_id],
            "requirement_results": [
                _requirement_result("q1:r1", "covered", finding_ids=[finding_a_id])
            ],
            "orientation_summary": "The first source establishes the assigned answer part.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.8,
        }
    )
    second_source = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S02",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=finding_b_id,
                    evidence_id=evidence_b_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )

    stale_outcome = state["workspace"]["coverage_outcomes"][0]
    assert first_source["accepted"] is True
    assert provisional["accepted"] is True
    assert second_source["accepted"] is True
    assert stale_outcome["is_stale"] is True
    assert "stale_outcome_question_ids" not in second_source

    refreshed = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "covered",
            "finding_ids": [finding_a_id, finding_b_id],
            "requirement_results": [
                _requirement_result(
                    "q1:r1",
                    "covered",
                    finding_ids=[finding_a_id, finding_b_id],
                )
            ],
            "orientation_summary": "Both sources jointly establish the assigned answer part.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.9,
        }
    )

    current_outcome = state["workspace"]["coverage_outcomes"][0]
    assert refreshed["accepted"] is True
    assert current_outcome["is_stale"] is False
    assert current_outcome["finding_ids"] == [finding_a_id, finding_b_id]


@pytest.mark.asyncio
async def test_outcome_rejects_finding_outside_requirement_result_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_1 = str(uuid.uuid4())
    evidence_2 = str(uuid.uuid4())
    workspace = _structured_workspace(
        source_refs=["S01"], assigned_requirement_ids=["q1:r1", "q1:r2"]
    )
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {
                evidence_1: _evidence(evidence_1, source_id),
                evidence_2: _evidence(evidence_2, source_id),
            },
            {source_id: "S01"},
        ),
        current_set_id="set_1",
    )
    finding_1 = "finding:S01:first"
    finding_2 = "finding:S01:second"
    await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=finding_1,
                    evidence_id=evidence_1,
                    requirement_ids=["q1:r1"],
                ),
                _finding_args(
                    finding_id=finding_2,
                    evidence_id=evidence_2,
                    requirement_ids=["q1:r2"],
                ),
            ],
        )
    )

    result = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "partial",
            "finding_ids": [finding_1, finding_2],
            "requirement_results": [
                _requirement_result("q1:r1", "covered", finding_ids=[finding_1]),
                _requirement_result("q1:r2", "missing"),
            ],
            "orientation_summary": "One part is established.",
            "missing_detail": "The second part remains absent.",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.7,
        }
    )

    assert result["accepted"] is False
    assert result["required_finding_ids"] == [finding_1]


@pytest.mark.asyncio
async def test_partial_outcome_cannot_defer_requirement_without_representation_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _structured_workspace(
        source_refs=["S01"], assigned_requirement_ids=["q1:r1", "q1:r2"]
    )
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)}, {source_id: "S01"}
        ),
        current_set_id="set_1",
    )
    finding_id = "finding:S01:covered"
    await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[],
            findings=[
                _finding_args(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    requirement_ids=["q1:r1"],
                )
            ],
        )
    )

    result = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "partial",
            "finding_ids": [finding_id],
            "requirement_results": [
                _requirement_result("q1:r1", "covered", finding_ids=[finding_id]),
                _requirement_result("q1:r2", "deferred"),
            ],
            "orientation_summary": "One part is established.",
            "missing_detail": "The second part is not established.",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.7,
        }
    )

    assert result["accepted"] is False
    assert result["requirement_ids"] == ["q1:r2"]
    assert "representation blocker" in result["error"]


@pytest.mark.asyncio
async def test_representation_recovery_stales_previous_deferred_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _structured_workspace(source_refs=["S01"], assigned_requirement_ids=["q1:r1"])
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)}, {source_id: "S01"}
        ),
        current_set_id="set_1",
    )
    unreadable = _source_analysis_args(
        source_ref="S01",
        evidence_ids=[],
        findings=[],
        no_findings_reason="No target text was available to inspect.",
    )
    unreadable.update(
        {
            "representation_status": "unreadable",
            "representation_reason": "No readable target text was returned.",
            "relevance": "unknown",
            "relevance_reason": "",
            "authority": "unknown",
            "authority_reason": "",
            "uniqueness": "unknown",
            "uniqueness_reason": "",
            "disposition": "deferred",
            "disposition_reason": "The source cannot be evaluated from this representation.",
            "editorial_functions": [],
        }
    )
    first_save = await handler.upsert_source_assessment_tool(unreadable)
    deferred = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "deferred",
            "finding_ids": [],
            "requirement_results": [_requirement_result("q1:r1", "deferred")],
            "orientation_summary": "",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "The source representation blocks evaluation.",
            "confidence": 0.5,
        }
    )
    recovered = await handler.upsert_source_assessment_tool(
        _source_analysis_args(
            source_ref="S01",
            evidence_ids=[evidence_id],
            findings=[],
            no_findings_reason="The readable source does not establish this answer requirement.",
        )
    )
    finish = await handler.finish_triage_source_set_tool(
        {"set_id": "set_1", "status": "mapped", "summary": "Reading is complete."}
    )

    assert first_save["accepted"] is True
    assert deferred["accepted"] is True
    assert recovered["accepted"] is True
    assert state["workspace"]["coverage_outcomes"][0]["is_stale"] is True
    assert finish["accepted"] is True


@pytest.mark.asyncio
async def test_resolved_readable_assessment_requires_and_selects_grounded_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    empty_evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            evidence_id: _evidence(evidence_id, source_id),
            empty_evidence_id: _evidence(empty_evidence_id, source_id, snippet=""),
        },
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )

    rejected = await handler.upsert_source_assessment_tool(_assessment_args([]))
    ungrounded = await handler.upsert_source_assessment_tool(_assessment_args([empty_evidence_id]))
    accepted = await handler.upsert_source_assessment_tool(_assessment_args([evidence_id]))

    assert rejected["accepted"] is False
    assert "requires grounded evidence" in rejected["error"]
    assert ungrounded["accepted"] is False
    assert ungrounded["invalid_evidence_ids"] == [empty_evidence_id]
    assert accepted["accepted"] is True
    assert toolset.evidence[evidence_id].metadata_["usage_status"] == "selected"
    assert toolset.selection_calls[0]["purpose"] == "source_assessment"
    assert state["workspace"]["source_sets"][0]["evidence_ids"] == [evidence_id]


@pytest.mark.asyncio
async def test_assessment_attaches_cross_question_relevance_and_invalidates_old_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    workspace["coverage_questions"].append({"question_id": "q2", "question": "What else is established?"})
    workspace["coverage_syntheses"] = [{"question_id": "q1", "status": "covered"}]
    workspace["source_map_finalization"] = {"ready": True}
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {evidence_id: _evidence(evidence_id, source_id)},
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )
    args = {**_assessment_args([evidence_id]), "coverage_question_ids": ["q1", "q2"], "confidence": 0}

    result = await handler.upsert_source_assessment_tool(args)

    assert result["accepted"] is True
    stored = state["workspace"]
    assert stored["source_sets"][0]["question_ids"] == ["q1"]
    assert stored["source_sets"][0]["assigned_question_ids"] == ["q1"]
    assert stored["source_sets"][0]["cross_question_ids"] == ["q2"]
    assert result["assigned_question_ids"] == ["q1"]
    assert "missing_outcome_question_ids" not in result
    # Legacy assessment-only writes do not count as a completed source analysis.
    assert result["pending_source_refs"] == ["S01"]
    assert stored["source_assessments"][0]["confidence"] == 0
    assert stored["coverage_syntheses"] == []
    assert "source_map_finalization" not in stored
    assert stored["triage_semantic_revision"] == 1


@pytest.mark.asyncio
async def test_replacing_assessment_detaches_obsolete_cross_question_and_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    workspace["coverage_questions"].append(
        {"question_id": "q2", "question": "What else is established?"}
    )
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {evidence_id: _evidence(evidence_id, source_id)},
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )

    first = await handler.upsert_source_assessment_tool(
        {**_assessment_args([evidence_id]), "coverage_question_ids": ["q1", "q2"]}
    )
    state["workspace"]["coverage_outcomes"] = [
        {"outcome_id": "outcome:q2:set_1", "set_id": "set_1", "question_id": "q2"}
    ]
    second = await handler.upsert_source_assessment_tool(
        {**_assessment_args([evidence_id]), "coverage_question_ids": ["q1"]}
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    stored_set = state["workspace"]["source_sets"][0]
    assert stored_set["planned_question_ids"] == ["q1"]
    assert stored_set["cross_question_ids"] == []
    assert stored_set["question_ids"] == ["q1"]
    assert state["workspace"]["coverage_outcomes"] == []


@pytest.mark.asyncio
async def test_assessment_rejects_active_role_for_unreadable_or_out_of_scope_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {evidence_id: _evidence(evidence_id, source_id)},
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )

    unreadable_args = _assessment_args([])
    unreadable_args.update(
        {
            "representation_status": "unreadable",
            "representation_reason": "The available representation contains no readable source text.",
        }
    )
    out_of_scope_args = _assessment_args([evidence_id])
    out_of_scope_args["relevance"] = "out_of_scope"

    unreadable = await handler.upsert_source_assessment_tool(unreadable_args)
    out_of_scope = await handler.upsert_source_assessment_tool(out_of_scope_args)

    assert unreadable["accepted"] is False
    assert out_of_scope["accepted"] is False
    assert "must use disposition=deferred" in unreadable["error"]
    assert "active research input" in out_of_scope["error"]


@pytest.mark.asyncio
async def test_unreadable_assessment_requires_explicit_deferred_disposition_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset({}, {source_id: "S01"}),
        current_set_id="set_1",
    )
    base = {
        **_assessment_args([]),
        "representation_status": "unreadable",
        "representation_reason": "The checked representation contains no readable source text.",
        "relevance": "unknown",
        "relevance_reason": "",
        "authority": "unknown",
        "authority_reason": "",
        "uniqueness": "unknown",
        "uniqueness_reason": "",
        "disposition": "deferred",
        "coverage_question_ids": [],
        "editorial_functions": [],
    }

    missing_reason = await handler.upsert_source_assessment_tool(base)
    accepted = await handler.upsert_source_assessment_tool(
        {**base, "disposition_reason": "Source value is unknown because its representation is unreadable."}
    )
    invalid_readable = await handler.upsert_source_assessment_tool(
        {
            **_assessment_args([]),
            "disposition": "deferred",
            "disposition_reason": "Do this later.",
        }
    )

    assert missing_reason["accepted"] is False
    assert "disposition_reason" in missing_reason["error"]
    assert accepted["accepted"] is True
    assert state["workspace"]["source_assessments"][0]["disposition_reason"].startswith(
        "Source value is unknown"
    )
    assert invalid_readable["accepted"] is False
    assert "partial, not_target_content, or unreadable" in invalid_readable["error"]


@pytest.mark.asyncio
async def test_not_target_content_persists_only_unknown_deferred_judgments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            evidence_id: _evidence(
                evidence_id,
                source_id,
                snippet="Cookie settings. Accept only necessary cookies. Accept all cookies.",
            )
        },
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )
    args = {
        **_assessment_args([evidence_id]),
        "representation_status": "not_target_content",
        "representation_reason": "Only a consent page was obtained; the requested document is absent.",
        "relevance": "unknown",
        "relevance_reason": "",
        "authority": "unknown",
        "authority_reason": "",
        "uniqueness": "unknown",
        "uniqueness_reason": "",
        "disposition": "deferred",
        "disposition_reason": "The unseen source cannot be evaluated from this service page.",
        "coverage_question_ids": [],
        "editorial_functions": [],
    }

    result = await handler.upsert_source_assessment_tool(args)

    assert result["accepted"] is True
    stored = state["workspace"]["source_assessments"][0]
    assert stored["representation_status"] == "not_target_content"
    assert stored["relevance"] == stored["authority"] == stored["uniqueness"] == "unknown"
    assert stored["disposition"] == "deferred"
    assert stored["coverage_question_ids"] == []
    assert stored["editorial_functions"] == []
    assert toolset.selection_calls[0]["purpose"] == "caution"


@pytest.mark.asyncio
async def test_not_target_content_rejects_source_judgments_links_and_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {evidence_id: _evidence(evidence_id, source_id)},
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )
    safe = {
        **_assessment_args([evidence_id]),
        "representation_status": "not_target_content",
        "representation_reason": "The target document was not obtained.",
        "relevance": "unknown",
        "relevance_reason": "",
        "authority": "unknown",
        "authority_reason": "",
        "uniqueness": "unknown",
        "uniqueness_reason": "",
        "disposition": "deferred",
        "disposition_reason": "Source value remains unknown.",
        "coverage_question_ids": [],
        "editorial_functions": [],
    }

    source_judgment = await handler.upsert_source_assessment_tool(
        {**safe, "uniqueness": "unique"}
    )
    semantic_reason = await handler.upsert_source_assessment_tool(
        {**safe, "authority_reason": "The service page itself appears weak."}
    )
    unfinished = await handler.upsert_source_assessment_tool(
        {**safe, "processing_status": "needs_repair"}
    )
    semantic_links = await handler.upsert_source_assessment_tool(
        {**safe, "coverage_question_ids": ["q1"], "editorial_functions": ["mechanism"]}
    )
    missing_evidence = await handler.upsert_source_assessment_tool(
        {**safe, "evidence_ids": []}
    )

    assert source_judgment["accepted"] is False
    assert source_judgment["invalid_fields"] == ["uniqueness"]
    assert semantic_reason["accepted"] is False
    assert semantic_reason["invalid_fields"] == ["authority_reason"]
    assert unfinished["accepted"] is False
    assert "processing_status=resolved" in unfinished["error"]
    assert semantic_links["accepted"] is False
    assert "cannot establish coverage-question" in semantic_links["error"]
    assert missing_evidence["accepted"] is False
    assert "requires grounded evidence" in missing_evidence["error"]


@pytest.mark.asyncio
async def test_shell_only_evidence_cannot_be_submitted_as_readable_target_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {
                evidence_id: _evidence(
                    evidence_id,
                    source_id,
                    snippet="Cookie settings. Accept only necessary cookies. Accept all cookies.",
                )
            },
            {source_id: "S01"},
        ),
        current_set_id="set_1",
    )

    result = await handler.upsert_source_assessment_tool(_assessment_args([evidence_id]))

    assert result["accepted"] is False
    assert result["required_representation_status"] == "not_target_content"
    assert result["invalid_evidence_ids"] == [evidence_id]


@pytest.mark.asyncio
async def test_shell_warning_does_not_reject_separately_grounded_target_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    shell_evidence_id = str(uuid.uuid4())
    content_evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {
                shell_evidence_id: _evidence(
                    shell_evidence_id,
                    source_id,
                    snippet="Cookie settings. Accept only necessary cookies. Accept all cookies.",
                ),
                content_evidence_id: _evidence(
                    content_evidence_id,
                    source_id,
                    snippet=(
                        "The requested report explains the monetary mechanism, identifies the responsible "
                        "institutions, and describes the sequence of operations in substantive detail."
                    ),
                ),
            },
            {source_id: "S01"},
        ),
        current_set_id="set_1",
    )

    result = await handler.upsert_source_assessment_tool(
        _assessment_args([shell_evidence_id, content_evidence_id])
    )

    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_targeted_repair_backend_rejects_writes_outside_exact_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01", "S02"], question_ids=["q1", "q2"])
    workspace["source_sets"][0].update(
        {
            "repair_targeted": True,
            "repair_source_refs": ["S01"],
            "repair_question_ids": ["q1"],
        }
    )
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[_card(source_1, "First"), _card(source_2, "Second")]
        ),
        evidence_toolset=_EvidenceToolset({}, {source_1: "S01", source_2: "S02"}),
        current_set_id="set_1",
    )

    assessment = await handler.upsert_source_assessment_tool(
        {**_assessment_args([]), "source_ref": "S02", "coverage_question_ids": ["q2"]}
    )
    outcome = await handler.upsert_coverage_outcome_tool(
        {
            **_outcome_args(status="missing", source_refs=[], evidence_ids=[]),
            "question_id": "q2",
            "orientation_summary": "",
        }
    )
    assert assessment["accepted"] is False
    assert "targeted repair scope" in assessment["error"]
    assert outcome["accepted"] is False
    assert "targeted repair scope" in outcome["error"]


@pytest.mark.asyncio
async def test_partial_outcome_requires_evidence_from_its_declared_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    evidence_1 = str(uuid.uuid4())
    evidence_2 = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01", "S02"])
    workspace["coverage_syntheses"] = [{"question_id": "q1", "status": "covered"}]
    workspace["source_map_finalization"] = {"ready": True}
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            evidence_1: _evidence(evidence_1, source_1),
            evidence_2: _evidence(evidence_2, source_2),
        },
        {source_1: "S01", source_2: "S02"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_1, "First"), _card(source_2, "Second")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )

    empty = await handler.upsert_coverage_outcome_tool(
        _outcome_args(status="partial", source_refs=["S01"], evidence_ids=[])
    )
    wrong_source = await handler.upsert_coverage_outcome_tool(
        _outcome_args(status="partial", source_refs=["S01"], evidence_ids=[evidence_2])
    )
    accepted = await handler.upsert_coverage_outcome_tool(
        {
            **_outcome_args(status="partial", source_refs=["S01"], evidence_ids=[evidence_1]),
            "confidence": 0,
        }
    )

    assert empty["accepted"] is False
    assert empty["error"] == "partial outcome requires saved evidence"
    assert wrong_source["accepted"] is False
    assert wrong_source["invalid_evidence_ids"] == [evidence_2]
    assert accepted["accepted"] is True
    assert toolset.evidence[evidence_1].metadata_["usage_status"] == "selected"
    stored = state["workspace"]
    assert stored["coverage_outcomes"][0]["confidence"] == 0
    assert stored["coverage_syntheses"] == []
    assert "source_map_finalization" not in stored


@pytest.mark.asyncio
async def test_per_set_outcome_cannot_request_run_wide_source_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset({}, {source_id: "S01"}),
        current_set_id="set_1",
    )

    result = await handler.upsert_coverage_outcome_tool(
        {
            **_outcome_args(status="missing", source_refs=[], evidence_ids=[]),
            "orientation_summary": "",
            "missing_detail": "This source set does not establish the answer.",
            "next_action": "source_acquisition",
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "per_set_outcome_cannot_route_run"


@pytest.mark.asyncio
async def test_source_acquisition_synthesis_waits_for_every_assigned_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    workspace.update(
        {
            "source_order": ["S01", "S02"],
            "source_sets": [
                {
                    "set_id": "set_1",
                    "status": "mapped",
                    "source_refs": ["S01"],
                    "question_ids": ["q1"],
                },
                {
                    "set_id": "set_2",
                    "status": "planned",
                    "source_refs": ["S02"],
                    "question_ids": ["q1"],
                },
            ],
            "coverage_outcomes": [
                {
                    "outcome_id": "outcome:q1:set_1",
                    "set_id": "set_1",
                    "question_id": "q1",
                    "status": "missing",
                    "source_refs": [],
                    "source_ids": [],
                    "evidence_ids": [],
                    "orientation_summary": "",
                    "missing_detail": "The first set does not contain the answer.",
                    "conflict_summary": "",
                    "deferred_reason": "",
                    "confidence": 0.8,
                    "next_action": "research",
                }
            ],
            "coverage_syntheses": [],
        }
    )
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[_card(source_1, "First"), _card(source_2, "Second")]
        ),
        evidence_toolset=_EvidenceToolset({}, {source_1: "S01", source_2: "S02"}),
    )
    args = {
        "question_id": "q1",
        "status": "missing",
        "source_refs": [],
        "evidence_ids": [],
        "orientation_summary": "",
        "missing_detail": "The completed corpus triage does not establish the answer.",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
        "next_action": "source_acquisition",
    }

    rejected = await handler.upsert_coverage_synthesis_tool(args)
    covered_rejected = await handler.upsert_coverage_synthesis_tool(
        {
            **args,
            "status": "covered",
            "orientation_summary": "The corpus already answers the question.",
            "missing_detail": "",
        }
    )
    state["workspace"]["source_sets"][1]["status"] = "mapped"
    state["workspace"]["coverage_outcomes"].append(
        {
            "outcome_id": "outcome:q1:set_2",
            "set_id": "set_2",
            "question_id": "q1",
            "status": "missing",
            "source_refs": [],
            "source_ids": [],
            "evidence_ids": [],
            "orientation_summary": "",
            "missing_detail": "The second set does not contain the answer.",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.8,
            "next_action": "research",
        }
    )
    accepted = await handler.upsert_coverage_synthesis_tool(args)
    state["workspace"]["coverage_outcomes"][1]["status"] = "covered"
    inconsistent_missing = await handler.upsert_coverage_synthesis_tool(args)

    assert rejected["accepted"] is False
    assert rejected["error"] == "source_acquisition_requires_all_assigned_sets_finished"
    assert rejected["unfinished_set_ids"] == ["set_2"]
    assert covered_rejected["accepted"] is False
    assert covered_rejected["error"] == "covered_synthesis_cannot_request_source_acquisition"
    assert accepted["accepted"] is True
    assert inconsistent_missing["accepted"] is True


@pytest.mark.asyncio
async def test_structured_synthesis_normalizes_question_route_from_requirement_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _structured_workspace(
        source_refs=["S01"],
        assigned_requirement_ids=["q1:r1", "q1:r2"],
    )
    workspace["source_order"] = ["S01"]
    workspace["source_sets"][0]["status"] = "mapped"
    workspace["coverage_outcomes"] = [
        {
            "outcome_id": "outcome:q1:set_1",
            "set_id": "set_1",
            "question_id": "q1",
            "status": "missing",
            "source_refs": [],
            "source_ids": [],
            "evidence_ids": [],
            "is_stale": False,
        }
    ]
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[]),
        evidence_toolset=_EvidenceToolset({}, {}),
    )

    route_cases = [
        (["none", "none"], "none"),
        (["omit", "none"], "omit"),
        (["research", "omit"], "research"),
        (["source_acquisition", "research"], "source_acquisition"),
    ]
    for routes, expected in route_cases:
        result = await handler.upsert_coverage_synthesis_tool(
            {
                "question_id": "q1",
                "requirement_results": [
                    {**_requirement_result("q1:r1", "missing"), "next_action": routes[0]},
                    {**_requirement_result("q1:r2", "missing"), "next_action": routes[1]},
                ],
                "status": "missing",
                "orientation_summary": "",
                "missing_detail": "The completed review does not establish either answer part.",
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.8,
                "next_action": "none",
            }
        )

        assert result["accepted"] is True
        assert state["workspace"]["coverage_syntheses"][0]["next_action"] == expected


@pytest.mark.asyncio
async def test_question_synthesis_reconciles_sets_and_rejects_contradictory_status_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    evidence_1 = str(uuid.uuid4())
    evidence_2 = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01", "S02"])
    workspace.update(
        {
            "source_order": ["S01", "S02"],
            "coverage_questions": [{"question_id": "q1", "question": "What is established?"}],
            "source_assessments": [
                {
                    **_assessment_args([evidence_1]),
                    "source_id": source_1,
                    "source_ref": "S01",
                },
                {
                    **_assessment_args([evidence_2]),
                    "source_id": source_2,
                    "source_ref": "S02",
                },
            ],
            "coverage_outcomes": [
                {
                    **_outcome_args(
                        status="partial",
                        source_refs=["S01", "S02"],
                        evidence_ids=[evidence_1, evidence_2],
                    ),
                    "source_ids": [source_1, source_2],
                }
            ],
            "coverage_syntheses": [],
        }
    )
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            evidence_1: _evidence(evidence_1, source_1),
            evidence_2: _evidence(evidence_2, source_2),
        },
        {source_1: "S01", source_2: "S02"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_1, "First"), _card(source_2, "Second")]),
        evidence_toolset=toolset,
    )
    base_args = {
        "question_id": "q1",
        "status": "covered",
        "source_refs": ["S01", "S02"],
        "evidence_ids": [evidence_1, evidence_2],
        "orientation_summary": "Together the two sets establish the answer.",
        "missing_detail": "",
        "conflict_summary": "A weaker account differed, but the official record resolves it.",
        "deferred_reason": "",
        "confidence": 0,
        "next_action": "research",
    }

    contradictory = await handler.upsert_coverage_synthesis_tool(
        {**base_args, "missing_detail": "The same answer is still absent."}
    )
    unbacked_source = await handler.upsert_coverage_synthesis_tool(
        {**base_args, "evidence_ids": [evidence_1]}
    )
    accepted = await handler.upsert_coverage_synthesis_tool(base_args)

    assert contradictory["accepted"] is False
    assert "contradict" in contradictory["error"]
    assert unbacked_source["accepted"] is False
    assert unbacked_source["error"] == "coverage_synthesis_source_refs_require_selected_evidence"
    assert unbacked_source["invalid_source_refs"] == ["S02"]
    assert accepted["accepted"] is True
    stored = state["workspace"]["coverage_syntheses"]
    assert len(stored) == 1
    assert stored[0]["source_ids"] == [source_1, source_2]
    assert stored[0]["orientation_summary"] == "Together the two sets establish the answer."
    assert stored[0]["confidence"] == 0
    assert stored[0]["input_hash"]
    assert stored[0]["input_revision"] == 0


@pytest.mark.asyncio
async def test_question_synthesis_derives_lineage_from_current_per_set_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    evidence_1 = str(uuid.uuid4())
    evidence_2 = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01", "S02"])
    second_assessment = {
        **_assessment_args([evidence_2]),
        "source_id": source_2,
        "source_ref": "S02",
    }
    workspace.update(
        {
            "source_order": ["S01", "S02"],
            "source_assessments": [
                {
                    **_assessment_args([evidence_1]),
                    "source_id": source_1,
                    "source_ref": "S01",
                },
                second_assessment,
            ],
            "coverage_outcomes": [
                {
                    **_outcome_args(
                        status="covered",
                        source_refs=["S01", "S02"],
                        evidence_ids=[evidence_1, evidence_2],
                    ),
                    "outcome_id": "outcome:q1:set_1",
                    "source_ids": [source_1, source_2],
                }
            ],
            "coverage_syntheses": [],
        }
    )
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            evidence_1: _evidence(evidence_1, source_1),
            evidence_2: _evidence(evidence_2, source_2),
        },
        {source_1: "S01", source_2: "S02"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_1, "First"), _card(source_2, "Second")]),
        evidence_toolset=toolset,
    )

    result = await handler.upsert_coverage_synthesis_tool(
        {
            "question_id": "q1",
            "status": "covered",
            "orientation_summary": "Together the accepted outcomes establish the answer.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.9,
            "next_action": "research",
        }
    )

    assert result["accepted"] is True
    assert result["lineage_derived_from_outcomes"] is True
    stored = state["workspace"]["coverage_syntheses"][0]
    assert stored["source_refs"] == ["S01", "S02"]
    assert stored["evidence_ids"] == [evidence_1, evidence_2]
    assert stored["outcome_refs"] == ["outcome:q1:set_1"]


@pytest.mark.asyncio
async def test_question_synthesis_cannot_borrow_other_question_assessment_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    evidence_q1 = str(uuid.uuid4())
    evidence_q2 = str(uuid.uuid4())
    evidence_q1_outcome = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01", "S02"], question_ids=["q1", "q2"])
    first_assessment = {
        **_assessment_args([evidence_q1]),
        "source_id": source_1,
        "source_ref": "S01",
        "coverage_question_ids": ["q1"],
    }
    second_assessment = {
        **_assessment_args([evidence_q2]),
        "source_id": source_2,
        "source_ref": "S02",
        "coverage_question_ids": ["q2"],
    }
    workspace.update(
        {
            "source_order": ["S01", "S02"],
            "source_assessments": [first_assessment, second_assessment],
            "coverage_outcomes": [
                {
                    **_outcome_args(
                        status="covered",
                        source_refs=["S01", "S02"],
                        evidence_ids=[evidence_q1, evidence_q1_outcome],
                    ),
                    "source_ids": [source_1, source_2],
                }
            ],
            "coverage_syntheses": [],
        }
    )
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            evidence_q1: _evidence(evidence_q1, source_1),
            evidence_q2: _evidence(evidence_q2, source_2),
            evidence_q1_outcome: _evidence(evidence_q1_outcome, source_2),
        },
        {source_1: "S01", source_2: "S02"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_1, "First"), _card(source_2, "Second")]),
        evidence_toolset=toolset,
    )
    args = {
        "question_id": "q1",
        "status": "covered",
        "source_refs": ["S02"],
        "orientation_summary": "The outcome-specific passage answers q1.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
        "next_action": "research",
    }

    rejected = await handler.upsert_coverage_synthesis_tool(
        {**args, "evidence_ids": [evidence_q2]}
    )
    accepted = await handler.upsert_coverage_synthesis_tool(
        {**args, "evidence_ids": [evidence_q1_outcome]}
    )

    assert rejected["accepted"] is False
    assert rejected["invalid_evidence_ids"] == [evidence_q2]
    assert accepted["accepted"] is True
    assert state["workspace"]["coverage_syntheses"][0]["evidence_ids"] == [evidence_q1_outcome]


@pytest.mark.asyncio
async def test_set_cannot_finalize_active_outcome_from_unusable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    workspace["source_assessments"] = [
        {
            **_assessment_args([evidence_id]),
            "source_id": source_id,
                "source_ref": "S01",
                "disposition": "unusable",
                "relevance_reason": "The source cannot support active research.",
                "no_findings_reason": "Reading found no material usable for the assigned question.",
            }
    ]
    workspace["coverage_outcomes"] = [
        {
            **_outcome_args(status="partial", source_refs=["S01"], evidence_ids=[evidence_id]),
            "source_ids": [source_id],
        }
    ]
    workspace["coverage_outcomes"][0]["workbench_snapshot_id"] = question_workbench_signature(
        workspace,
        workspace["source_sets"][0],
        question_id="q1",
    )
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
        ),
        current_set_id="set_1",
    )

    result = await handler.finish_triage_source_set_tool(
        {"set_id": "set_1", "status": "mapped", "summary": "The set is mapped."}
    )

    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_needs_repair_cannot_end_an_incomplete_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01", "S02"])
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[_card(source_1, "First"), _card(source_2, "Second")]
        ),
        evidence_toolset=_EvidenceToolset({}, {source_1: "S01", source_2: "S02"}),
        current_set_id="set_1",
    )

    result = await handler.finish_triage_source_set_tool(
        {
            "set_id": "set_1",
            "status": "needs_repair",
            "summary": "The set is incomplete.",
            "deferred_reason": "S02 still needs to be assessed.",
            "deferred_sources": [],
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "status must be mapped or deferred"
    assert result["next_action"]
    assert state["workspace"]["source_sets"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_repair_reupsert_recomputes_set_evidence_without_stale_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    old_evidence_id = str(uuid.uuid4())
    new_evidence_id = str(uuid.uuid4())
    workspace = _workspace(source_refs=["S01"])
    workspace["current_set"] = {"set_id": "set_1"}
    workspace["source_sets"].insert(
        0,
        {
            "set_id": "old_set",
            "source_refs": ["S01"],
            "question_ids": [],
            "observations": [],
            "written_actions": [],
            "evidence_ids": [old_evidence_id],
        },
    )
    workspace["source_assessments"] = [
        {
            "set_id": "old_set",
            "source_id": source_id,
            "source_ref": "S01",
            "evidence_ids": [old_evidence_id],
        }
    ]
    workspace["source_sets"][1]["evidence_ids"] = [old_evidence_id]
    state = _patch_workspace(monkeypatch, workspace)
    toolset = _EvidenceToolset(
        {
            old_evidence_id: _evidence(old_evidence_id, source_id),
            new_evidence_id: _evidence(new_evidence_id, source_id),
        },
        {source_id: "S01"},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
        evidence_toolset=toolset,
        current_set_id="set_1",
    )

    result = await handler.upsert_source_assessment_tool(_assessment_args([new_evidence_id]))

    evidence_by_set = {item["set_id"]: item["evidence_ids"] for item in state["workspace"]["source_sets"]}
    assert result["accepted"] is True
    assert evidence_by_set == {"old_set": [], "set_1": [new_evidence_id]}


@pytest.mark.asyncio
async def test_quote_action_and_source_map_ignore_model_supplied_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    evidence = _evidence(evidence_id, source_id, locator="canonical section 4")

    async def no_workspace_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(write_actions, "mark_workspace_write_action", no_workspace_write)
    owner = SimpleNamespace(db=None, run=SimpleNamespace(id=uuid.uuid4()))
    owner._evidence_in_run = lambda _evidence_id: None

    async def evidence_in_run(_evidence_id):
        return evidence

    async def source_ref_for_id(_source_id):
        return "S01"

    async def no_scope_error(_refs):
        return None

    async def select_evidence(_ids, **_kwargs):
        return []

    owner._evidence_in_run = evidence_in_run
    owner._source_ref_for_id = source_ref_for_id
    owner._triage_scope_error_for_refs = no_scope_error
    owner._triage_write_scope_error_for_refs = no_scope_error
    owner._select_evidence_ids_for_write = select_evidence
    quote_args = {
        "evidence_id": evidence_id,
        "quote": evidence.snippet_text.upper(),
        "locator": "invented page 999",
        "narrative_use": "mechanism",
        "confidence": 0.8,
        "confidence_reason": "Exact source window.",
    }
    action_result = await write_actions.create_quote_card(owner, quote_args)
    evidence.locator_text = ""
    missing_locator = await write_actions.create_quote_card(
        owner,
        {
            "evidence_id": evidence_id,
            "quote": evidence.snippet_text,
            "narrative_use": "mechanism",
            "confidence": 0.8,
            "confidence_reason": "Exact source window.",
        },
    )
    evidence.locator_text = "canonical section 4"

    traces = [
        SimpleNamespace(
            tool_name="upsert_source_card",
            input_json={
                "source_id": source_id,
                "summary": "Grounded source.",
                "useful_for": ["mechanism"],
                "evidence_ids": [evidence_id],
                "gaps_or_cautions": [],
            },
        ),
        SimpleNamespace(
            tool_name="create_quote_card",
            input_json={
                "evidence_id": evidence_id,
                "quote": evidence.snippet_text,
                "locator": "invented page 999",
                "narrative_use": "mechanism",
                "confidence": 0.8,
                "confidence_reason": "Exact source window.",
            },
        ),
    ]
    source_map = await build_source_map_from_write_actions(
        _WriteActionDb(traces, [evidence]),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=SourceMap(source_cards=[_card(source_id, "First")]),
    )

    assert action_result["accepted"] is True
    assert action_result["locator"] == "canonical section 4"
    assert quote_args["quote"] == evidence.snippet_text
    assert missing_locator["accepted"] is False
    assert source_map.quote_cards[0].locator == "canonical section 4"


@pytest.mark.asyncio
async def test_resolved_gap_leaves_history_but_not_current_source_map() -> None:
    source_id = str(uuid.uuid4())
    inventory = SourceMap(source_cards=[_card(source_id, "First")])
    record = SimpleNamespace(
        tool_name="record_gap",
        input_json={
            "kind": "coverage",
            "description": "The mechanism is not established.",
            "source_refs": ["S01"],
            "evidence_ids": [],
        },
        output_json={"accepted": True},
    )
    finalize = SimpleNamespace(
        tool_name="finalize_source_map",
        input_json={"ready": True, "summary": "Done."},
        output_json={"accepted": True},
    )
    first = await build_source_map_from_write_actions(
        _WriteActionDb([record, finalize], []),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )
    resolve = SimpleNamespace(
        tool_name="resolve_gap",
        input_json={
            "gap_id": first.gap_records[0].gap_id,
            "gap_ref": "G01",
            "reason": "A later durable read established the mechanism.",
        },
        output_json={"accepted": True},
    )

    current = await build_source_map_from_write_actions(
        _WriteActionDb([record, resolve, finalize], []),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )

    assert first.gaps == ["The mechanism is not established."]
    assert current.gap_records == []
    assert current.gaps == []
    assert current.source_triage.gaps == []


@pytest.mark.asyncio
async def test_gap_refs_remain_stable_when_an_earlier_gap_is_resolved() -> None:
    source_id = str(uuid.uuid4())
    inventory = SourceMap(source_cards=[_card(source_id, "First")])
    records = [
        SimpleNamespace(
            tool_name="record_gap",
            input_json={
                "kind": "coverage",
                "description": description,
                "source_refs": ["S01"],
                "evidence_ids": [],
            },
            output_json={"accepted": True},
        )
        for description in ("First gap.", "Second gap.")
    ]
    before = await build_source_map_from_write_actions(
        _WriteActionDb(records, []),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )
    refs_before = {item.description: write_actions.public_gap_ref(item.gap_id) for item in before.gap_records}
    resolve_first = SimpleNamespace(
        tool_name="resolve_gap",
        input_json={"gap_id": before.gap_records[0].gap_id},
        output_json={"accepted": True},
    )

    after = await build_source_map_from_write_actions(
        _WriteActionDb([*records, resolve_first], []),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )

    assert [item.description for item in after.gap_records] == ["Second gap."]
    assert write_actions.public_gap_ref(after.gap_records[0].gap_id) == refs_before["Second gap."]


@pytest.mark.asyncio
async def test_legacy_invalid_relationship_remains_blocking_until_explicit_repair_trace() -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(source_cards=[_card(source_id, "First")])
    evidence = _evidence(evidence_id, source_id)
    evidence.metadata_["usage_status"] = "selected"
    invalid_relationship = SimpleNamespace(
        tool_name="record_source_relationship",
        input_json={
            "source_ref": "S01",
            "related_source_ref": "S01",
            "relationship": "duplicate",
            "evidence_ids": [evidence_id],
            "rationale": "Invalid legacy self-link.",
        },
        output_json={"accepted": True},
    )
    before = await build_source_map_from_write_actions(
        _WriteActionDb([invalid_relationship], [evidence]),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )
    repair = SimpleNamespace(
        tool_name="resolve_source_map_repair",
        input_json={
            "target_kind": "relationship",
            "source_id": source_id,
            "related_source_id": source_id,
            "relationship": "duplicate",
        },
        output_json={"accepted": True},
    )
    after = await build_source_map_from_write_actions(
        _WriteActionDb([invalid_relationship, repair], [evidence]),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )

    assert len(before.source_triage.source_relationships) == 1
    assert after.source_triage.source_relationships == []


@pytest.mark.asyncio
async def test_structural_repair_does_not_delete_a_newer_valid_replacement() -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[_card(source_id_1, "First"), _card(source_id_2, "Second")]
    )
    evidence = _evidence(evidence_id, source_id_1)
    evidence.metadata_["usage_status"] = "selected"
    invalid_record = SourceRelationship(
        source_id=source_id_1,
        related_source_id=source_id_2,
        relationship="duplicate",
        evidence_ids=[],
        rationale="Ungrounded legacy record.",
    )
    invalid_trace = SimpleNamespace(
        tool_name="record_source_relationship",
        input_json={
            "source_ref": "S01",
            "related_source_ref": "S02",
            "relationship": "duplicate",
            "evidence_ids": [],
            "rationale": invalid_record.rationale,
        },
        output_json={"accepted": True},
    )
    replacement_trace = SimpleNamespace(
        tool_name="record_source_relationship",
        input_json={
            "source_ref": "S01",
            "related_source_ref": "S02",
            "relationship": "duplicate",
            "evidence_ids": [evidence_id],
            "rationale": "Grounded replacement.",
        },
        output_json={"accepted": True},
    )
    repair_trace = SimpleNamespace(
        tool_name="resolve_source_map_repair",
        input_json={
            "target_kind": "relationship",
            "source_id": source_id_1,
            "related_source_id": source_id_2,
            "relationship": "duplicate",
            "target_fingerprint": source_map_record_fingerprint("relationship", invalid_record),
        },
        output_json={"accepted": True},
    )

    source_map = await build_source_map_from_write_actions(
        _WriteActionDb([invalid_trace, replacement_trace, repair_trace], [evidence]),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )

    assert len(source_map.source_triage.source_relationships) == 1
    assert source_map.source_triage.source_relationships[0].evidence_ids == [evidence_id]
    assert source_map.source_triage.source_relationships[0].rationale == "Grounded replacement."


@pytest.mark.asyncio
async def test_source_map_repair_requires_explicit_resolution_before_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "triage_semantic_revision": 1,
        "source_map_repair_items": [
            {
                "repair_ref": "SMR01",
                "status": "open",
                "kind": "relationship",
                "issues": ["self_relationship"],
                "source_refs": ["S01"],
            }
        ],
        "source_map_repair_targets": {
            "SMR01": {
                "target_kind": "relationship",
                "source_id": source_id,
                "related_source_id": source_id,
                "relationship": "duplicate",
            }
        },
    }
    saved: list[dict] = []

    async def load_workspace(_db, _run_id):
        return workspace

    async def save_workspace(_db, *, workspace, **_kwargs):
        saved.append(workspace)
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.save_source_triage_workspace",
        save_workspace,
    )
    owner = SimpleNamespace(
        db=None,
        run=SimpleNamespace(id=uuid.uuid4()),
        _selected_evidence_ids=lambda: None,
    )

    blocked = await write_actions.finalize_source_map(owner, {"ready": True, "summary": "Not repaired."})
    repair_args = {"repair_ref": "SMR01", "reason": "Discard the invalid self-link."}
    resolved = await write_actions.resolve_source_map_repair(owner, repair_args)

    assert blocked == {
        "accepted": False,
        "error": "source_map_repairs_open",
        "repair_refs": ["SMR01"],
    }
    assert workspace["source_map_repair_items"][0]["status"] == "resolved"
    assert resolved == {"accepted": True, "repair_ref": "SMR01", "resolved": True}
    assert repair_args["source_id"] == source_id
    assert source_id not in str(resolved)
    assert saved


@pytest.mark.asyncio
async def test_source_map_finalization_does_not_require_finalizer_review_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {
        "schema_version": "source_triage_workspace.v3",
        "coverage_questions": [
            {
                "question_id": "q-main",
                "question": "Why?",
                "answer_requirements": [],
            }
        ],
        "source_findings": [
            {
                "finding_id": "finding:S01:one",
                "source_ref": "S01",
                "statement": "A durable finding.",
            }
        ],
        "source_map_repair_items": [],
    }

    async def load_workspace(_db, _run_id):
        return workspace

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    owner = SimpleNamespace(db=None, run=SimpleNamespace(id=uuid.uuid4()))

    result = await write_actions.finalize_source_map(
        owner,
        {"ready": True, "summary": "Premature."},
    )

    assert result["accepted"] is False
    assert result["error"] == "source_map_workspace_not_ready"
    assert "finalizer_review_stream_incomplete" not in {
        item["code"] for item in result["violations"]
    }


@pytest.mark.asyncio
async def test_resolve_gap_rejects_reason_without_durable_evidence_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_workspace(_db, _run_id):
        return {"source_assessments": []}

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    owner = SimpleNamespace(db=None, run=SimpleNamespace(id=uuid.uuid4()))

    result = await write_actions.resolve_gap(
        owner,
        {
            "gap_ref": "G-1234567890",
            "basis_ref": "assessment:S01",
            "reason": "Trust me.",
        },
    )

    assert result["accepted"] is False
    assert "saved evidence" in result["error"]


@pytest.mark.asyncio
async def test_resolve_gap_uses_compact_ref_and_persists_internal_identity_only_in_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    gap = SourceGapRecord(
        gap_id="gap:internal-hash",
        kind="coverage",
        description="An old gap.",
        source_ids=[source_id],
    )

    async def current_map(_owner):
        inventory = SourceMap(source_cards=[_card(source_id, "First")])
        return SourceMap(source_cards=inventory.source_cards, gap_records=[gap]), inventory

    async def no_workspace_write(*_args, **_kwargs):
        return None

    async def source_ref_for_id(_source_id):
        return "S01"

    async def no_scope_error(_refs):
        return None

    async def load_workspace(_db, _run_id):
        return {
            "source_assessments": [
                {
                    "source_ref": "S01",
                    "processing_status": "resolved",
                    "evidence_ids": [evidence_id],
                }
            ]
        }

    async def evidence_in_run(requested_evidence_id):
        return (
            SimpleNamespace(id=requested_evidence_id, source_id=uuid.UUID(source_id))
            if requested_evidence_id == evidence_id
            else None
        )

    monkeypatch.setattr(write_actions, "_current_source_map", current_map)
    monkeypatch.setattr(write_actions, "mark_workspace_write_action", no_workspace_write)
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    owner = SimpleNamespace(
        db=None,
        run=SimpleNamespace(id=uuid.uuid4()),
        triage_current_set_id="set_1",
        _source_ref_for_id=source_ref_for_id,
        _triage_write_scope_error_for_refs=no_scope_error,
        _evidence_in_run=evidence_in_run,
    )
    gap_ref = write_actions.public_gap_ref(gap.gap_id)
    args = {
        "gap_ref": gap_ref,
        "basis_ref": "assessment:S01",
        "reason": "The repaired evidence closes it.",
    }

    result = await write_actions.resolve_gap(owner, args)

    assert result == {
        "accepted": True,
        "gap_ref": gap_ref,
        "basis_ref": "assessment:S01",
        "resolved": True,
    }
    assert "gap_id" not in result
    assert args["gap_id"] == "gap:internal-hash"
    assert args["basis_evidence_ids"] == [evidence_id]


@pytest.mark.asyncio
async def test_resolve_gap_rejects_evidence_basis_from_unrelated_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gap_source_id = str(uuid.uuid4())
    basis_source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    gap = SourceGapRecord(
        gap_id="gap:source-one",
        kind="coverage",
        description="Coverage for the first source remains open.",
        source_ids=[gap_source_id],
    )

    async def current_map(_owner):
        inventory = SourceMap(
            source_cards=[_card(gap_source_id, "First"), _card(basis_source_id, "Second")]
        )
        return SourceMap(source_cards=inventory.source_cards, gap_records=[gap]), inventory

    async def source_ref_for_id(source_id):
        return "S01" if str(source_id) == gap_source_id else "S02"

    async def no_scope_error(_refs):
        return None

    async def load_workspace(_db, _run_id):
        return {
            "source_assessments": [
                {
                    "source_ref": "S02",
                    "source_id": basis_source_id,
                    "processing_status": "resolved",
                    "coverage_question_ids": ["q_other"],
                    "evidence_ids": [evidence_id],
                }
            ]
        }

    async def evidence_in_run(requested_evidence_id):
        return SimpleNamespace(id=requested_evidence_id) if requested_evidence_id == evidence_id else None

    monkeypatch.setattr(write_actions, "_current_source_map", current_map)
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    owner = SimpleNamespace(
        db=None,
        run=SimpleNamespace(id=uuid.uuid4()),
        triage_current_set_id="set_1",
        _source_ref_for_id=source_ref_for_id,
        _triage_write_scope_error_for_refs=no_scope_error,
        _evidence_in_run=evidence_in_run,
    )

    result = await write_actions.resolve_gap(
        owner,
        {
            "gap_ref": write_actions.public_gap_ref(gap.gap_id),
            "basis_ref": "assessment:S02",
            "reason": "Unrelated material should not close the gap.",
        },
    )

    assert result["accepted"] is False
    assert "not linked" in result["error"]


@pytest.mark.asyncio
async def test_resolve_gap_with_question_lineage_rejects_same_source_other_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    gap = SourceGapRecord(
        gap_id="gap:question-one",
        kind="coverage",
        description="The first question remains open.",
        source_ids=[source_id],
        question_ids=["q1"],
    )

    async def current_map(_owner):
        inventory = SourceMap(source_cards=[_card(source_id, "First")])
        return SourceMap(source_cards=inventory.source_cards, gap_records=[gap]), inventory

    async def source_ref_for_id(_source_id):
        return "S01"

    async def no_scope_error(_refs):
        return None

    async def load_workspace(_db, _run_id):
        return {
            "source_assessments": [
                {
                    "source_ref": "S01",
                    "source_id": source_id,
                    "processing_status": "resolved",
                    "coverage_question_ids": ["q2"],
                    "evidence_ids": [evidence_id],
                }
            ]
        }

    async def evidence_in_run(requested_evidence_id):
        return (
            SimpleNamespace(id=requested_evidence_id, source_id=uuid.UUID(source_id))
            if requested_evidence_id == evidence_id
            else None
        )

    monkeypatch.setattr(write_actions, "_current_source_map", current_map)
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_persistence.load_source_triage_workspace",
        load_workspace,
    )
    owner = SimpleNamespace(
        db=None,
        run=SimpleNamespace(id=uuid.uuid4()),
        triage_current_set_id="set_1",
        _source_ref_for_id=source_ref_for_id,
        _triage_write_scope_error_for_refs=no_scope_error,
        _evidence_in_run=evidence_in_run,
    )

    result = await write_actions.resolve_gap(
        owner,
        {
            "gap_ref": write_actions.public_gap_ref(gap.gap_id),
            "basis_ref": "assessment:S01",
            "reason": "Same source but the wrong question must not close the gap.",
        },
    )

    assert result["accepted"] is False
    assert "question_id" in result["error"]


@pytest.mark.asyncio
async def test_source_triage_gate_rejects_legacy_single_source_cluster() -> None:
    source_id = str(uuid.uuid4())
    source_map = SourceMap(
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="legacy_singleton",
                    label="Not actually a cluster",
                    source_ids=[source_id],
                )
            ]
        ),
        retrieval_notes=["Legacy map finalized."],
    )

    gate = await validate_source_map_gate(
        _WriteActionDb([], []),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[],
        ),
    )

    assert "cluster:membership:legacy_singleton" in gate.ungrounded_relationships
    assert gate.passed is False


def test_research_source_map_assessment_page_hides_internal_source_uuid() -> None:
    source_id = str(uuid.uuid4())
    assessment = SourceAssessment(
        assessment_id=f"assessment:{source_id}",
        source_id=source_id,
        processing_status="resolved",
        representation_status="readable",
        disposition="anchor",
    )
    source_map = SourceMap(
        source_cards=[_card(source_id, "First")],
        source_assessments=[assessment],
    )

    page = source_map_research_page(
        source_map,
        source_map,
        section="source_map_assessments",
    )

    serialized = str(page)
    assert page["items"][0]["assessment_ref"] == "assessment:S01"
    assert source_id not in serialized


def test_quote_tool_contract_does_not_ask_the_model_for_a_locator() -> None:
    class _Owner:
        db = None
        run = SimpleNamespace(id=uuid.uuid4())

        def __getattr__(self, _name):
            async def handler(_args):
                return {"accepted": True}

            return handler

    quote_tool = next(tool for tool in evidence_write_tools(_Owner()) if tool.name == "create_quote_card")

    assert "locator" not in quote_tool.parameters["properties"]
    assert "locator" not in quote_tool.parameters["required"]


def test_read_window_and_relationship_tool_contracts_use_canonical_inputs() -> None:
    class _Owner:
        db = None
        run = SimpleNamespace(id=uuid.uuid4())

        def __getattr__(self, _name):
            async def handler(_args):
                return {"accepted": True}

            return handler

    owner = _Owner()
    read_tool = next(tool for tool in evidence_source_read_tools(owner) if tool.name == "read_source_window")
    relationship_tool = next(
        tool for tool in evidence_write_tools(owner) if tool.name == "record_source_relationship"
    )

    assert "window_ref" in read_tool.parameters["properties"]
    assert "version_ref" not in read_tool.parameters["properties"]
    assert {"window_ref", "locator"}.issubset(read_tool.parameters["required"])
    assert relationship_tool.parameters["properties"]["relationship"]["enum"] == [
        "primary_to_secondary",
        "duplicate",
        "near_duplicate",
        "contradicts",
        "updates",
        "background_for",
    ]
