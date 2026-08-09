from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.generation_v2.pipeline.source_triage_source_mapper_flow import source_mapper_evidence_tools
from app.generation_v2.research_workspace import _jobs_from_corpus_brief
from app.generation_v2.schemas import (
    CorpusBrief,
    CorpusBriefIntent,
    CoverageQuestion,
    CoverageRequirement,
    SourceAnalysis,
    SourceAnalysisReadState,
    SourceCard,
    SourceMap,
)
from app.generation_v2.source_triage_canonical import canonical_workspace_violations
from app.generation_v2.source_triage_source_analysis_state import (
    clear_source_mapping_terminal_failure,
    has_source_mapping_terminal_failure,
    record_source_mapping_terminal_failure,
)
from app.generation_v2.source_triage_workspace_state import READ_RECEIPT_DELIVERY_DELIVERED
from app.generation_v2.source_triage_workspace_tool_handlers import SourceTriageWorkspaceToolHandlers
from app.generation_v2.source_triage_plan_validation import validate_triage_plan
from app.generation_v2.source_triage_research_handoff import build_source_mapping_research_handoff


def test_source_analysis_incomplete_read_state_requires_boundaries() -> None:
    with pytest.raises(ValidationError):
        SourceAnalysisReadState(complete=False)

    assert SourceAnalysisReadState(
        complete=False,
        unread_regions=["appendix"],
        stopping_reason="Appendix contains only bibliography.",
    ).complete is False


def test_source_mapper_evidence_surface_is_read_source_only() -> None:
    class Toolset:
        def agent_tools(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(name="read_source"),
                SimpleNamespace(name="search_source"),
                SimpleNamespace(name="read_source_window"),
                SimpleNamespace(name="persist_evidence"),
            ]

    assert [tool.name for tool in source_mapper_evidence_tools(Toolset())] == ["read_source"]


@pytest.mark.asyncio
async def test_source_mapper_analysis_materializes_read_source_passage_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = {
        "source_order": ["S01"],
        "source_sets": [{"set_id": "set-1", "status": "in_progress", "source_refs": ["S01"]}],
        "coverage_questions": [{"question_id": "q_leaf", "question": "Leaf?"}],
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:S01:eof",
                "set_id": "set-1",
                "source_ref": "S01",
                "read_kind": "sequential",
                "eof": True,
                "delivery_status": READ_RECEIPT_DELIVERY_DELIVERED,
            }
        ],
        "source_analyses": [],
        "source_analysis_history": [],
        "source_assessments": [],
        "source_findings": [],
        "mapped_sources": [],
    }
    saved: dict[str, object] = {}

    class EvidenceToolset:
        async def materialize_source_passages(self, **kwargs):
            assert kwargs["source_ref"] == "S01"
            assert kwargs["passage_refs"] == ["passage:S01:1"]
            assert kwargs["allowed_read_kinds"] == {"sequential"}
            return {"accepted": True, "evidence_ids": ["ev-1"]}

        async def _evidence_in_run(self, evidence_id: str):
            assert evidence_id == "ev-1"
            return SimpleNamespace(source_id="source-1", snippet_text="Grounded source text.")

        async def _source_ref_for_id(self, source_id: str) -> str:
            assert source_id == "source-1"
            return "S01"

        async def _select_evidence_ids_for_write(self, evidence_ids: list[str], **_kwargs) -> list[str]:
            assert evidence_ids == ["ev-1"]
            return []

        def _evidence_has_current_exact_read(self, _evidence) -> bool:
            return True

    async def load_workspace(_db, _run_id):
        return workspace

    async def save_workspace(_db, *, workspace, **_kwargs):
        saved["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace.save_source_triage_workspace",
        save_workspace,
    )

    handler = SourceTriageWorkspaceToolHandlers(
        db=SimpleNamespace(),
        run=SimpleNamespace(id="run-1", project_id="project-1"),
        workspace=workspace,
        inventory=SourceMap(source_cards=[_source_card("source-1", "S01")]),
        evidence_toolset=EvidenceToolset(),
        current_set_id="set-1",
    )

    result = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "usable",
            "status_reason": "",
            "summary": "Useful source.",
            "source_limitations": [],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [
                {
                    "statement": "The source establishes a salient fact.",
                    "evidence_ids": [],
                    "passage_refs": ["passage:S01:1"],
                    "question_ids": ["q_leaf"],
                    "limitations": [],
                }
            ],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )

    assert result["accepted"] is True
    analysis = saved["workspace"]["source_analyses"][0]
    assert analysis["findings"][0]["evidence_ids"] == ["ev-1"]
    assert saved["workspace"]["source_findings"][0]["passage_refs"] == ["passage:S01:1"]


@pytest.mark.asyncio
async def test_source_mapper_complete_true_requires_backend_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _mapper_workspace("S01", "source-1")
    handler = _handler(monkeypatch, workspace=workspace, source_id="source-1", source_ref="S01")

    result = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "usable",
            "status_reason": "",
            "summary": "Useful source.",
            "source_limitations": [],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "read_state.complete requires delivered read_source EOF"


@pytest.mark.asyncio
async def test_source_mapper_deferred_accepts_backend_terminal_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _mapper_workspace("S01", "source-1")
    workspace["source_mapping_terminal_failures"] = [
        {
            "set_id": "set-1",
            "source_ref": "S01",
            "error": "source_has_no_text_readable_representation",
            "reason": "Native/PDF representation has no extracted text.",
        }
    ]
    handler = _handler(monkeypatch, workspace=workspace, source_id="source-1", source_ref="S01")

    saved = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "deferred",
            "status_reason": "Backend has no readable text representation for this source.",
            "summary": "",
            "source_limitations": ["Native file needs text extraction before mapping."],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )
    finished = await handler.finish_source_mapping_tool({"source_ref": "S01", "set_id": "set-1"})

    assert saved["accepted"] is True
    assert finished["accepted"] is True
    assert workspace["deferred_sources"][0]["source_ref"] == "S01"


@pytest.mark.asyncio
async def test_source_mapper_deferred_rejects_retryable_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _mapper_workspace("S01", "source-1")
    record_source_mapping_terminal_failure(
        workspace,
        set_id="set-1",
        source_ref="S01",
        error="source_text_storage_temporarily_unavailable",
        reason="Temporary object storage error.",
        retryable=True,
    )
    handler = _handler(monkeypatch, workspace=workspace, source_id="source-1", source_ref="S01")

    result = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "deferred",
            "status_reason": "Temporary storage failure.",
            "summary": "",
            "source_limitations": ["Retry later."],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )

    assert has_source_mapping_terminal_failure(workspace, set_id="set-1", source_ref="S01") is False
    assert result["accepted"] is False
    assert result["error"] == "read_state.complete requires delivered read_source EOF"


def test_successful_source_read_can_clear_terminal_failure() -> None:
    workspace = _mapper_workspace("S01", "source-1")
    record_source_mapping_terminal_failure(
        workspace,
        set_id="set-1",
        source_ref="S01",
        error="source_has_no_text_readable_representation",
        reason="No extracted text existed before retry.",
    )
    assert has_source_mapping_terminal_failure(workspace, set_id="set-1", source_ref="S01") is True

    clear_source_mapping_terminal_failure(workspace, set_id="set-1", source_ref="S01")

    assert has_source_mapping_terminal_failure(workspace, set_id="set-1", source_ref="S01") is False


@pytest.mark.asyncio
async def test_source_mapper_allows_readable_irrelevant_source_without_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _mapper_workspace("S01", "source-1")
    workspace["source_read_receipts"] = [_eof_receipt("S01")]
    handler = _handler(monkeypatch, workspace=workspace, source_id="source-1", source_ref="S01")

    saved = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "usable",
            "status_reason": "Readable source, but it contains no salient material for the leaf questions.",
            "summary": "Administrative page unrelated to the research questions.",
            "source_limitations": [],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )
    finished = await handler.finish_source_mapping_tool({"source_ref": "S01", "set_id": "set-1"})

    assert saved["accepted"] is True
    assert finished["accepted"] is True
    assert workspace["source_assessments"][0]["no_findings_reason"].startswith("Readable source")


@pytest.mark.asyncio
async def test_source_mapper_preserves_model_read_boundaries_after_backend_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _mapper_workspace("S01", "source-1")
    workspace["source_read_receipts"] = [_eof_receipt("S01")]
    handler = _handler(monkeypatch, workspace=workspace, source_id="source-1", source_ref="S01")

    saved = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "usable",
            "status_reason": "Readable source; appendix was not semantically reviewed.",
            "summary": "Partial semantic mapping of a readable source.",
            "source_limitations": [],
            "read_state": {
                "complete": False,
                "unread_regions": ["Appendix tables"],
                "stopping_reason": "Appendix tables looked unrelated to source mapping.",
            },
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )

    assert saved["accepted"] is True
    read_state = workspace["source_analyses"][0]["read_state"]
    assert read_state["complete"] is False
    assert read_state["backend_eof"] is True
    assert read_state["unread_regions"] == ["Appendix tables"]


@pytest.mark.asyncio
async def test_source_mapper_not_target_syncs_deferred_index_and_canonical_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _mapper_workspace("S01", "source-1")
    workspace["source_read_receipts"] = [
        {
            "receipt_ref": "receipt:S01:eof",
            "set_id": "set-1",
            "source_ref": "S01",
            "read_kind": "sequential",
            "eof": True,
            "delivery_status": READ_RECEIPT_DELIVERY_DELIVERED,
        }
    ]
    handler = _handler(monkeypatch, workspace=workspace, source_id="source-1", source_ref="S01")

    result = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S01",
            "status": "not_target_content",
            "status_reason": "The delivered text is a login shell, not the target document.",
            "summary": "",
            "source_limitations": ["No target document text is available."],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "",
        }
    )

    assert result["accepted"] is True
    assert workspace["deferred_sources"][0]["source_ref"] == "S01"
    violation_codes = {item["code"] for item in canonical_workspace_violations(workspace)}
    assert "deferred_source_index_mismatch" not in violation_codes
    assert "inconsistent_source_assessment" not in violation_codes


@pytest.mark.asyncio
async def test_source_mapper_exact_duplicate_can_finish_without_repeating_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _mapper_workspace("S02", "source-2")
    workspace["source_order"] = ["S01", "S02"]
    workspace["exact_duplicate_sources"] = {"S02": "S01"}
    handler = _handler(
        monkeypatch,
        workspace=workspace,
        source_id="source-2",
        source_ref="S02",
        inventory=SourceMap(source_cards=[_source_card("source-1", "S01"), _source_card("source-2", "S02")]),
    )

    saved = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S02",
            "status": "usable",
            "status_reason": "",
            "summary": "Backend verified this source is byte-identical to S01.",
            "source_limitations": ["Duplicate content; use S01 findings for substance."],
            "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
            "findings": [],
            "gaps": [],
            "duplicate_of_source_ref": "S01",
        }
    )
    finished = await handler.finish_source_mapping_tool({"source_ref": "S02", "set_id": "set-1"})

    assert saved["accepted"] is True
    assert finished["accepted"] is True
    assert workspace["source_assessments"][0]["duplicate_of_source_ref"] == "S01"


def test_deterministic_handoff_uses_full_source_analysis_content_without_candidate_fields() -> None:
    questions = _questions()
    source_card = _source_card("source-03", "S03")
    workspace = {
        "source_analyses": [
            SourceAnalysis(
                analysis_id="analysis:S03",
                source_ref="S03",
                source_id="source-03",
                set_id="set-1",
                status="usable",
                summary="HKMA history page.",
                read_state=SourceAnalysisReadState(complete=True),
                findings=[
                    {
                        "finding_id": "finding:S03:1",
                        "statement": "HK$10 private notes were replaced with coins, public demand continued, and HKMA issued its own HK$10 notes.",
                        "evidence_ids": ["ev-hk10"],
                        "question_ids": ["q_current"],
                        "limitations": ["The source explains demand, not the original private issuance legal basis."],
                    }
                ],
                gaps=[
                    {
                        "question_id": "q_origin",
                        "description": "The source describes later HK$10 policy but not the early legal basis for private issuance.",
                    }
                ],
            ).model_dump(mode="json")
        ],
    }

    handoff = build_source_mapping_research_handoff(
        questions=questions,
        workspace=workspace,
        source_cards_by_ref={"S03": source_card},
    )

    by_question = {item.question_ids[0]: item for item in handoff}
    current = by_question["q_current"]
    assert current.findings[0]["statement"].startswith("HK$10 private notes")
    assert current.evidence_ids == ["ev-hk10"]
    assert current.source_refs == ["S03"]
    assert current.allowed_source_ids == []
    assert current.preferred_source_ids == []
    assert current.candidate_source_ids == []
    assert current.action == "none"
    assert by_question["q_origin"].gaps[0]["source_ref"] == "S03"
    assert "q_root" not in by_question


def test_deterministic_handoff_keeps_same_gap_text_from_different_sources() -> None:
    questions = _questions()
    workspace = {
        "source_analyses": [
            SourceAnalysis(
                analysis_id="analysis:S01",
                source_ref="S01",
                source_id="source-01",
                set_id="set-1",
                status="usable",
                summary="Source one.",
                status_reason="Readable but no findings for this question.",
                read_state=SourceAnalysisReadState(complete=True),
                findings=[],
                gaps=[
                    {
                        "question_id": "q_origin",
                        "description": "The source does not identify the early legal basis.",
                    }
                ],
            ).model_dump(mode="json"),
            SourceAnalysis(
                analysis_id="analysis:S02",
                source_ref="S02",
                source_id="source-02",
                set_id="set-1",
                status="usable",
                summary="Source two.",
                status_reason="Readable but no findings for this question.",
                read_state=SourceAnalysisReadState(complete=True),
                findings=[],
                gaps=[
                    {
                        "question_id": "q_origin",
                        "description": "The source does not identify the early legal basis.",
                    }
                ],
            ).model_dump(mode="json"),
        ],
    }

    handoff = build_source_mapping_research_handoff(
        questions=questions,
        workspace=workspace,
        source_cards_by_ref={
            "S01": _source_card("source-01", "S01"),
            "S02": _source_card("source-02", "S02"),
        },
    )

    origin = next(item for item in handoff if item.question_ids == ["q_origin"])
    assert [gap["source_ref"] for gap in origin.gaps] == ["S01", "S02"]


def test_research_workspace_seed_uses_new_full_handoff_and_does_not_create_root_job() -> None:
    source_card = _source_card("source-03", "S03")
    handoff = build_source_mapping_research_handoff(
        questions=_questions(),
        workspace={
            "source_analyses": [
                {
                    "analysis_id": "analysis:S03",
                    "source_ref": "S03",
                    "source_id": "source-03",
                    "set_id": "set-1",
                    "status": "usable",
                    "summary": "HKMA history page.",
                    "source_limitations": [],
                    "read_state": {"complete": True, "unread_regions": [], "stopping_reason": ""},
                    "findings": [
                        {
                            "finding_id": "finding:S03:1",
                            "statement": "HKMA issued its own HK$10 notes after demand persisted.",
                            "evidence_ids": ["ev-hk10"],
                            "question_ids": ["q_current"],
                            "limitations": [],
                        }
                    ],
                    "gaps": [],
                    "current": True,
                    "accepted": True,
                }
            ],
        },
        source_cards_by_ref={"S03": source_card},
    )
    brief = CorpusBrief(
        generation_run_id="run-1",
        project_id="project-1",
        run_corpus_view_id="view-1",
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id="source-map-1",
        source_map_hash="hash",
        intent=CorpusBriefIntent(user_prompt="Explain Hong Kong banknotes."),
        coverage_questions=_questions(),
        source_findings=[],
        source_analyses=[],
        source_sets=[],
        coverage_outcomes=[],
        research_handoff=handoff,
    )

    jobs = _jobs_from_corpus_brief(
        brief,
        SourceMap(source_cards=[source_card]),
        allowed_source_refs=["S03"],
    )

    assert {tuple(job["coverage_question_ids"]) for job in jobs} == {("q_origin",), ("q_current",)}
    current = next(job for job in jobs if job["coverage_question_ids"] == ["q_current"])
    assert current["source_findings"][0]["statement"] == "HKMA issued its own HK$10 notes after demand persisted."
    assert current["source_refs"] == ["S03"]
    assert current["starting_evidence_ids"] == ["ev-hk10"]
    assert current["allow_broad_search"] is True


def test_root_requirements_do_not_require_set_level_delegation() -> None:
    workspace = {
        "source_order": ["S01"],
        "central_question_id": "q_root",
        "coverage_questions": [
            {
                "question_id": "q_root",
                "question": "Root?",
                "importance": "critical",
                "answer_requirements": [
                    {
                        "requirement_id": "q_root:r1",
                        "description": "Root requirement",
                        "rationale": "",
                        "completion_criterion": "Resolved by leaf.",
                    }
                ],
            },
            {
                "question_id": "q_leaf",
                "question": "Leaf?",
                "parent_question_id": "q_root",
                "answer_requirements": [
                    {
                        "requirement_id": "q_leaf:r1",
                        "description": "Leaf requirement",
                        "rationale": "",
                        "completion_criterion": "Evidence exists.",
                    }
                ],
            },
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": ["q_leaf"],
                "planned_question_ids": ["q_leaf"],
                "question_assignments": [{"question_id": "q_leaf", "requirement_ids": ["q_leaf:r1"]}],
            }
        ],
    }

    report = validate_triage_plan(workspace, central_question_id="q_root")
    assert report["valid"] is True
    assert not any("root requirements" in issue for issue in report["issues"])


def _questions() -> list[CoverageQuestion]:
    return [
        CoverageQuestion(
            question_id="q_root",
            question="Why does Hong Kong money work this way?",
            importance="critical",
            answer_requirements=[
                CoverageRequirement(
                    requirement_id="q_root:r1",
                    description="Synthesize the institutional explanation.",
                    completion_criterion="Leaf answers can be synthesized.",
                    synthesis_only=True,
                )
            ],
        ),
        CoverageQuestion(
            question_id="q_origin",
            question="What was the early legal basis for private note issuance?",
            parent_question_id="q_root",
            answer_requirements=[
                CoverageRequirement(
                    requirement_id="q_origin:r1",
                    description="Find the early legal basis.",
                    completion_criterion="Direct source identifies the basis.",
                    delegated_root_requirement_ids=["q_root:r1"],
                )
            ],
        ),
        CoverageQuestion(
            question_id="q_current",
            question="How does current HK$10 issuance work?",
            parent_question_id="q_root",
            answer_requirements=[
                CoverageRequirement(
                    requirement_id="q_current:r1",
                    description="Explain HK$10 issuance.",
                    completion_criterion="Source identifies issuer and reason.",
                    delegated_root_requirement_ids=["q_root:r1"],
                )
            ],
        ),
    ]


def _source_card(source_id: str, source_ref: str) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[f"{source_id}:v1"],
        title=f"{source_ref} title",
        source_type="web",
        url="https://example.com",
        domain="example.com",
        summary=f"{source_ref} summary",
    )


def _mapper_workspace(source_ref: str, source_id: str) -> dict:
    return {
        "source_order": [source_ref],
        "source_sets": [{"set_id": "set-1", "status": "in_progress", "source_refs": [source_ref]}],
        "coverage_questions": [{"question_id": "q_leaf", "question": "Leaf?"}],
        "source_read_receipts": [],
        "source_analyses": [],
        "source_analysis_history": [],
        "source_assessments": [],
        "source_findings": [],
        "mapped_sources": [],
        "deferred_sources": [],
    }


def _eof_receipt(source_ref: str) -> dict:
    return {
        "receipt_ref": f"receipt:{source_ref}:eof",
        "set_id": "set-1",
        "source_ref": source_ref,
        "read_kind": "sequential",
        "eof": True,
        "delivery_status": READ_RECEIPT_DELIVERY_DELIVERED,
    }


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: dict,
    source_id: str,
    source_ref: str,
    inventory: SourceMap | None = None,
) -> SourceTriageWorkspaceToolHandlers:
    class EvidenceToolset:
        async def _select_evidence_ids_for_write(self, _evidence_ids: list[str], **_kwargs) -> list[str]:
            return []

    async def load_workspace(_db, _run_id):
        return workspace

    async def save_workspace(_db, *, workspace, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace.save_source_triage_workspace",
        save_workspace,
    )
    return SourceTriageWorkspaceToolHandlers(
        db=SimpleNamespace(),
        run=SimpleNamespace(id="run-1", project_id="project-1"),
        workspace=workspace,
        inventory=inventory or SourceMap(source_cards=[_source_card(source_id, source_ref)]),
        evidence_toolset=EvidenceToolset(),
        current_set_id="set-1",
    )
