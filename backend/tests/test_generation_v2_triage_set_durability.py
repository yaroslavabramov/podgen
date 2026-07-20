from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2 import source_triage_read_checkpointing as tracking
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_triage_workspace_tool_handlers import (
    SourceTriageWorkspaceToolHandlers,
)
from app.generation_v2.source_triage_workspace_tool_specs import source_triage_set_tools
from app.models.generation_v2 import EvidenceStrength


class _EvidenceToolset:
    def __init__(
        self,
        evidence: dict[str, SimpleNamespace],
        refs: dict[str, str],
        *,
        exact_read_ids: set[str] | None = None,
    ) -> None:
        self.evidence = evidence
        self.refs = refs
        self.exact_read_ids = exact_read_ids

    async def _evidence_in_run(self, evidence_id: str):
        return self.evidence.get(evidence_id)

    async def _source_ref_for_id(self, source_id) -> str:
        return self.refs.get(str(source_id), "S??")

    async def _select_evidence_ids_for_write(self, _evidence_ids: list[str], **_kwargs):
        return []

    def _evidence_has_current_exact_read(self, evidence: SimpleNamespace) -> bool:
        return self.exact_read_ids is None or str(evidence.id) in self.exact_read_ids


def _evidence(evidence_id: str, source_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID(evidence_id),
        source_id=uuid.UUID(source_id),
        source_version_id=uuid.uuid4(),
        snippet_text="Exact grounded source text for the bounded finding.",
        locator_text="section 1",
        retrieval_channel="local_chunk_search",
        evidence_strength=EvidenceStrength.EXACT,
        metadata_={"usage_status": "observed"},
        purpose="source_read",
    )


def _workspace() -> dict:
    return {
        "schema_version": "source_triage_workspace.v3",
        "current_set": {"set_id": "set-1"},
        "source_order": ["S01", "S02"],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "in_progress",
                "source_refs": ["S01", "S02"],
                "planned_question_ids": ["q1"],
                "question_ids": ["q1"],
                "cross_question_ids": [],
                "question_assignments": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"]}
                ],
                "observations": [],
                "written_actions": [],
                "evidence_ids": [],
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
        "source_assessments": [],
        "source_findings": [],
        "coverage_outcomes": [],
        "triage_semantic_revision": 0,
    }


def _card(source_id: str, title: str) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[str(uuid.uuid4())],
        title=title,
        source_type="web",
        summary=f"{title} summary.",
    )


def _finding(
    *,
    finding_id: str,
    evidence_id: str,
    question_links: list[dict],
    source_relations: list[dict] | None = None,
) -> dict:
    return {
        "finding_id": finding_id,
        "statement": "The source establishes one bounded fact.",
        "evidence_ids": [evidence_id],
        "scope": {"actor": "Named actor", "period": "Named period", "geography": "", "qualifiers": []},
        "limitations": [],
        "question_links": question_links,
        "source_relations": source_relations or [],
    }


def _analysis_args(*, source_ref: str, findings: list[dict]) -> dict:
    return {
        "set_id": "set-1",
        "source_ref": source_ref,
        "processing_status": "resolved",
        "representation_status": "readable",
        "representation_reason": "The target text was read.",
        "relevance": "core",
        "relevance_reason": "It addresses a persisted question.",
        "authority": "official",
        "authority_reason": "It is an institutional publication.",
        "uniqueness": "unique",
        "uniqueness_reason": "It contributes an independently grounded fact.",
        "disposition": "anchor",
        "disposition_reason": "",
        "editorial_functions": [],
        "evidence_ids": [],
        "limitations": [],
        "confidence": 0.8,
        "findings": findings,
        "no_findings_reason": "",
    }


def _outcome_args(*, question_id: str, requirement_id: str, finding_id: str) -> dict:
    return {
        "set_id": "set-1",
        "question_id": question_id,
        "status": "covered",
        "finding_ids": [finding_id],
        "requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "covered",
                "finding_ids": [finding_id],
                "summary": "The answer part is established.",
                "missing_detail": "",
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.8,
            }
        ],
        "orientation_summary": "The answer is established.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
    }


def _patch_workspace(monkeypatch: pytest.MonkeyPatch, workspace: dict) -> dict:
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    return state


def test_set_relation_tool_contract_requires_two_sided_evidence() -> None:
    async def handler(_args):
        return {"accepted": True}

    analysis_tool = next(
        tool
        for tool in source_triage_set_tools(handler, handler, handler)
        if tool.name == "save_source_analysis"
    )
    relation_evidence = analysis_tool.parameters["properties"]["findings"]["items"][
        "properties"
    ]["source_relations"]["items"]["properties"]["evidence_ids"]

    assert relation_evidence["minItems"] == 2
    assert "at least one item from current source_ref" in relation_evidence["description"]


@pytest.mark.asyncio
async def test_exact_read_requires_a_durable_checkpoint_before_source_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace()
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace

    monkeypatch.setattr(tracking, "load_source_triage_workspace", load)
    monkeypatch.setattr(tracking, "save_source_triage_workspace", save)
    run_id = uuid.uuid4()

    advisory = await tracking.record_triage_exact_read(
        None,  # type: ignore[arg-type]
        generation_run_id=run_id,
        current_set_id="set-1",
        source_ref="S01",
        evidence_ids=["evidence-1"],
    )
    assert advisory is not None
    assert advisory["pending_source_refs"] == ["S01"]
    assert advisory["analysis_pending"] is True
    assert "before switching sources" in advisory["message"].casefold()

    tracking.clear_pending_exact_read(workspace["source_sets"][0], source_ref="S01")
    assert tracking.pending_exact_read_source_refs(
        workspace,
        source_set=workspace["source_sets"][0],
    ) == []


@pytest.mark.asyncio
async def test_set_relation_requires_evidence_from_both_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a, source_b = str(uuid.uuid4()), str(uuid.uuid4())
    evidence_a = str(uuid.uuid4())
    workspace = _workspace()
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_a, "A"), _card(source_b, "B")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_a: _evidence(evidence_a, source_a)},
            {source_a: "S01", source_b: "S02"},
        ),
        current_set_id="set-1",
    )

    result = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id="finding:S01:one",
                    evidence_id=evidence_a,
                    question_links=[
                        {
                            "question_id": "q1",
                            "requirement_ids": ["q1:r1"],
                            "rationale": "Direct support.",
                        }
                    ],
                    source_relations=[
                        {
                            "related_source_ref": "S02",
                            "relationship": "duplicate",
                            "rationale": "The texts are asserted to be the same.",
                            "evidence_ids": [evidence_a],
                        }
                    ],
                )
            ],
        )
    )

    assert result["accepted"] is False
    assert result["error"] == "A source relation requires grounded non-shell evidence from both sources"
    assert result["missing_evidence_source_refs"] == ["S02"]
    assert result["finding_index"] == 0


@pytest.mark.asyncio
async def test_search_preview_cannot_be_persisted_as_finding_before_exact_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_order"] = ["S01"]
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
            exact_read_ids=set(),
        ),
        current_set_id="set-1",
    )

    result = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id="finding:S01:one",
                    evidence_id=evidence_id,
                    question_links=[
                        {
                            "question_id": "q1",
                            "requirement_ids": ["q1:r1"],
                            "rationale": "Direct support.",
                        }
                    ],
                )
            ],
        )
    )

    assert result["accepted"] is False
    assert result["error"] == "Semantic triage evidence must have a current substantive exact read"
    assert result["not_exact_read_evidence_ids"] == [evidence_id]
    assert "read_evidence or read_source_window" in result["next_action"]


@pytest.mark.asyncio
async def test_discovered_question_attempt_requires_durable_link_or_explicit_reanalysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_sets"][0]["pending_exact_reads"] = [
        {"source_ref": "S01", "evidence_ids": [evidence_id]}
    ]
    workspace["source_order"] = ["S01"]
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
        ),
        current_set_id="set-1",
    )
    finding_id = "finding:S01:one"
    q1_link = {
        "question_id": "q1",
        "requirement_ids": ["q1:r1"],
        "rationale": "Direct support.",
    }
    saved = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[q1_link],
                )
            ],
        )
    )
    rejected = await handler.upsert_coverage_outcome_tool(
        _outcome_args(question_id="q2", requirement_id="q2:r1", finding_id=finding_id)
    )

    assert saved["accepted"] is True
    assert state["workspace"]["source_sets"][0]["pending_exact_reads"] == []
    assert rejected["accepted"] is False
    assert rejected["lineage_errors"] == [
        {
            "question_id": "q2",
            "requirement_id": "q2:r1",
            "finding_id": finding_id,
            "source_ref": "S01",
            "reason": "finding_not_linked_to_requirement",
            "corrective_action": (
                "Resave this source's complete analysis with the finding linked to the exact question and "
                "requirement; otherwise remove this finding from the outcome."
            ),
        }
    ]
    from app.generation_v2.runtime_tool_output import model_facing_tool_output

    assert model_facing_tool_output("upsert_coverage_outcome", rejected) == rejected
    pending = state["workspace"]["source_sets"][0]["pending_discovered_question_attempts"]
    assert pending[0]["question_id"] == "q2"
    assert pending[0]["source_refs"] == ["S01"]

    blocked_finish = await handler.finish_triage_source_set_tool(
        {
            "set_id": "set-1",
            "status": "mapped",
            "summary": "The set is mapped.",
            "deferred_reason": "",
            "deferred_sources": [],
        }
    )
    assert blocked_finish["accepted"] is False
    assert blocked_finish["pending_question_attempts"][0]["question_id"] == "q2"
    assert blocked_finish["pending_question_attempts"][0]["unreviewed_source_refs"] == ["S01"]

    confirmed_absent = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[q1_link],
                )
            ],
        )
    )
    assert confirmed_absent["accepted"] is True
    assert confirmed_absent["analysis_changed"] is False
    assert state["workspace"]["source_sets"][0]["pending_discovered_question_attempts"] == []
    assert "q2" not in state["workspace"]["source_sets"][0]["question_ids"]

    corrected = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[
                        q1_link,
                        {
                            "question_id": "q2",
                            "requirement_ids": ["q2:r1"],
                            "rationale": "The same passage also establishes the causal answer part.",
                        },
                    ],
                )
            ],
        )
    )
    accepted = await handler.upsert_coverage_outcome_tool(
        _outcome_args(question_id="q2", requirement_id="q2:r1", finding_id=finding_id)
    )

    assert corrected["accepted"] is True
    assert "q2" in state["workspace"]["source_sets"][0]["question_ids"]
    assert state["workspace"]["source_sets"][0]["pending_discovered_question_attempts"] == []
    assert accepted["accepted"] is True
