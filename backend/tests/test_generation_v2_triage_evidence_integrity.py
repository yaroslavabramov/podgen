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
    assert stored["source_sets"][0]["question_ids"] == ["q1", "q2"]
    assert stored["source_sets"][0]["cross_question_ids"] == ["q2"]
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
        "disposition": "deferred",
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
    assert "partial or unreadable" in invalid_readable["error"]


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
    observation = await handler.record_triage_observation_tool(
        {
            "set_id": "set_1",
            "source_refs": ["S02"],
            "evidence_ids": [],
            "note": "Outside repair scope.",
            "pending_questions": [],
        }
    )

    assert assessment["accepted"] is False
    assert "targeted repair scope" in assessment["error"]
    assert outcome["accepted"] is False
    assert "targeted repair scope" in outcome["error"]
    assert observation["accepted"] is False
    assert "targeted repair scope" in observation["error"]


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
    accepted = await handler.upsert_coverage_synthesis_tool(base_args)

    assert contradictory["accepted"] is False
    assert "contradict" in contradictory["error"]
    assert accepted["accepted"] is True
    stored = state["workspace"]["coverage_syntheses"]
    assert len(stored) == 1
    assert stored[0]["source_ids"] == [source_1, source_2]
    assert stored[0]["orientation_summary"] == "Together the two sets establish the answer."
    assert stored[0]["confidence"] == 0
    assert stored[0]["input_hash"]
    assert stored[0]["input_revision"] == 0


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
        }
    ]
    workspace["coverage_outcomes"] = [
        {
            **_outcome_args(status="partial", source_refs=["S01"], evidence_ids=[evidence_id]),
            "source_ids": [source_id],
        }
    ]
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

    assert result["accepted"] is False
    assert "unusable" in result["error"]


@pytest.mark.asyncio
async def test_needs_repair_can_preserve_incomplete_set_without_finalizing_it(
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

    assert result["accepted"] is True
    assert result["status"] == "needs_repair"
    assert state["workspace"]["source_sets"][0]["status"] == "needs_repair"


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
    assert "locator" not in read_tool.parameters["required"]
    assert relationship_tool.parameters["properties"]["relationship"]["enum"] == [
        "primary_to_secondary",
        "duplicate",
        "near_duplicate",
        "contradicts",
        "updates",
        "background_for",
    ]
