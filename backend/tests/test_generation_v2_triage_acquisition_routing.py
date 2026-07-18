from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.source_acquisition_agent import _model_gap_report
from app.generation_v2.contracts.pipeline import V2PipelineStep
from app.generation_v2.corpus_brief_actions import (
    corpus_brief_acquisition_requests,
    triage_state_ready_for_research,
)
from app.generation_v2.pipeline import source_acquisition_flow
from app.generation_v2.research_workspace import _jobs_from_corpus_brief
from app.generation_v2.schemas import (
    CorpusBrief,
    CorpusBriefIntent,
    CoverageOutcome,
    CoverageQuestion,
    CoverageRequirement,
    CoverageRequirementResult,
    SourceAcquisitionCompletion,
    SourceAcquisitionRound,
    SourceGapRecord,
    SourceMap,
    SourceTriageCompletion,
    SourceTriageGate,
)
from app.generation_v2.source_acquisition_gaps import build_corpus_gap_report
from app.generation_v2.source_triage_canonical import (
    audit_corpus_brief,
    materialize_corpus_brief,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context


def _brief(
    next_action: str,
    *,
    question: str = "Which legal trigger changed the mechanism?",
    reason: str = "The current corpus does not identify the trigger.",
) -> CorpusBrief:
    return CorpusBrief(
        generation_run_id="run-1",
        project_id="project-1",
        run_corpus_view_id="view-1",
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id="source-map-1",
        source_map_hash="hash",
        intent=CorpusBriefIntent(user_prompt="Explain the mechanism."),
        coverage_questions=[
            CoverageQuestion(
                question_id="q_trigger",
                question=question,
                kind="mechanism",
                importance="critical",
            )
        ],
        coverage_outcomes=[
            CoverageOutcome(
                outcome_id="outcome:q_trigger",
                question_id="q_trigger",
                status="missing",
                missing_detail=reason,
                next_action=next_action,
            )
        ],
    )


def _triage_state(next_action: str) -> SimpleNamespace:
    return SimpleNamespace(
        corpus=object(),
        source_inventory=SourceMap(),
        source_map=SourceMap(),
        source_workspace_artifact_ids=["triage-artifact"],
        triage_completion=SourceTriageCompletion(ready=True, summary="ready"),
        triage_gate=SourceTriageGate(passed=True, summary="passed"),
        corpus_brief=_brief(next_action),
    )


def _run(*, source_acquisition_mode: str = "auto", step: str | None = None) -> SimpleNamespace:
    config: dict[str, object] = {
        "source_acquisition_mode": source_acquisition_mode,
        "duration_target": 1_800,
    }
    if step is not None:
        config.update({"execution_mode": "step", "v2_stop_after_step": step})
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        show_bible_id=None,
        config=config,
        current_phase=None,
        current_step=None,
        progress_pct=None,
    )


def test_corpus_brief_acquisition_gap_keeps_exact_question_and_reason() -> None:
    question = "Which legal trigger changed  the mechanism?\nPreserve this wording."
    reason = "The selected material gives no trigger.\nNeed the primary instrument."
    brief = _brief("source_acquisition", question=question, reason=reason)

    report = build_corpus_gap_report(
        _run(),  # type: ignore[arg-type]
        trigger_phase="source_triage",
        round_number=1,
        source_map=SourceMap(),
        triage_ready=True,
        triage_gate=SourceTriageGate(passed=True, summary="passed"),
        corpus_brief=brief,
    )

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert str(gap.kind) == "triage_acquisition"
    assert gap.origin == "corpus_brief_next_action"
    assert gap.question_id == "q_trigger"
    assert gap.question == question
    assert gap.reason == reason
    assert gap.required_editorial_functions == ["mechanism"]
    assert str(gap.severity) == "high"
    model_gap = _model_gap_report(report, build_source_ref_context(SourceMap()))["gaps"][0]
    assert model_gap["question"] == question
    assert model_gap["reason"] == reason


def test_corpus_brief_route_does_not_compete_with_unrouted_source_map_gaps() -> None:
    brief = _brief("source_acquisition")
    source_map = SourceMap(
        gaps=["A general caution that belongs in later research."],
        gap_records=[
            SourceGapRecord(
                gap_id="gap-general",
                kind="caution",
                description="A second unrouted source-map caution.",
            )
        ],
    )

    report = build_corpus_gap_report(
        _run(),  # type: ignore[arg-type]
        trigger_phase="source_triage",
        round_number=1,
        source_map=source_map,
        triage_ready=True,
        triage_gate=SourceTriageGate(passed=True, summary="passed"),
        corpus_brief=brief,
    )

    assert [str(gap.kind) for gap in report.gaps] == ["triage_acquisition"]
    assert report.gaps[0].question_id == "q_trigger"


def test_corpus_brief_without_acquisition_route_does_not_invent_generic_gap() -> None:
    report = build_corpus_gap_report(
        _run(),  # type: ignore[arg-type]
        trigger_phase="source_triage",
        round_number=1,
        source_map=SourceMap(gaps=["Research this later."]),
        triage_ready=False,
        triage_gate=SourceTriageGate(passed=False, summary="failed"),
        corpus_brief=_brief("research"),
    )

    assert report.should_acquire is False
    assert report.gaps == []


def test_structured_source_map_gap_keeps_sources_and_supersedes_legacy_text() -> None:
    description = "The primary instrument establishing the mechanism is missing."
    source_map = SourceMap(
        gaps=[f"  {description}  "],
        gap_records=[
            SourceGapRecord(
                gap_id="gap-mechanism",
                kind="mechanism",
                description=description,
                source_ids=["source-1"],
            )
        ],
    )

    report = build_corpus_gap_report(
        _run(),  # type: ignore[arg-type]
        trigger_phase="source_triage",
        round_number=1,
        source_map=source_map,
        triage_ready=True,
        triage_gate=SourceTriageGate(passed=True, summary="passed"),
    )

    assert len(report.gaps) == 1
    assert report.gaps[0].description == description
    assert report.gaps[0].related_source_ids == ["source-1"]
    assert report.gaps[0].origin == "source_map_gap_record:mechanism"


@pytest.mark.parametrize("next_action", ["source_acquisition", "none", "omit"])
def test_non_research_next_action_creates_no_handoff_or_research_job(next_action: str) -> None:
    source_map = SourceMap()
    workspace = {
        "intent": {"user_prompt": "Explain the mechanism."},
        "coverage_questions": [
            {
                "question_id": "q_trigger",
                "question": "Which legal trigger changed the mechanism?",
                "kind": "mechanism",
                "importance": "critical",
            }
        ],
        "source_sets": [],
        "source_assessments": [],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome:q_trigger",
                "question_id": "q_trigger",
                "status": "missing",
                "missing_detail": "The current corpus does not identify the trigger.",
                "next_action": next_action,
            }
        ],
    }
    brief = materialize_corpus_brief(
        generation_run_id="run-1",
        project_id="project-1",
        run_corpus_view_id="view-1",
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id="source-map-1",
        source_map=source_map,
        workspace=workspace,
    )
    audit = audit_corpus_brief(
        corpus_brief_artifact_id="brief-1",
        brief=brief,
        source_map=source_map,
        selected_source_ids=set(),
        expected_generation_run_id="run-1",
        expected_project_id="project-1",
        expected_run_corpus_view_id="view-1",
        expected_corpus_revision=1,
        expected_attempt=1,
        expected_source_map_artifact_id="source-map-1",
    )

    assert brief.research_handoff == []
    assert _jobs_from_corpus_brief(brief, source_map) == []
    assert audit.passed is True


def test_pending_acquisition_action_is_not_ready_for_research() -> None:
    assert triage_state_ready_for_research(_triage_state("source_acquisition")) is False
    assert triage_state_ready_for_research(_triage_state("research")) is True
    assert triage_state_ready_for_research(_triage_state("none")) is True
    assert triage_state_ready_for_research(_triage_state("omit")) is True


def test_requirement_actions_route_each_answer_part_independently() -> None:
    question = CoverageQuestion(
        question_id="q_parts",
        question="Which parts are known and which still need work?",
        answer_requirements=[
            CoverageRequirement(requirement_id="r_research", description="Deep-read the current evidence."),
            CoverageRequirement(requirement_id="r_acquire", description="Find the missing primary instrument."),
            CoverageRequirement(requirement_id="r_omit", description="Drop the irrelevant branch."),
            CoverageRequirement(requirement_id="r_none", description="Keep the completed part closed."),
        ],
    )
    workspace = {
        "intent": {"user_prompt": "Resolve the composite question."},
        "coverage_questions": [question.model_dump(mode="json")],
        "source_sets": [],
        "source_assessments": [],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome:q_parts",
                "question_id": "q_parts",
                "status": "partial",
                "requirement_results": [
                    CoverageRequirementResult(
                        requirement_id="r_research",
                        status="partial",
                        summary="The current evidence establishes only part of the answer.",
                        missing_detail="The current evidence still needs deeper comparison.",
                        next_action="research",
                    ).model_dump(mode="json"),
                    CoverageRequirementResult(
                        requirement_id="r_acquire",
                        status="missing",
                        missing_detail="The primary instrument is absent from the corpus.",
                        next_action="source_acquisition",
                    ).model_dump(mode="json"),
                    CoverageRequirementResult(
                        requirement_id="r_omit",
                        status="missing",
                        missing_detail="This branch is irrelevant to the requested answer.",
                        next_action="omit",
                    ).model_dump(mode="json"),
                    CoverageRequirementResult(
                        requirement_id="r_none",
                        status="covered",
                        summary="No further work is needed for this part.",
                        next_action="none",
                    ).model_dump(mode="json"),
                ],
                "orientation_summary": "One answer part is established.",
                "missing_detail": "Other parts have explicit downstream routes.",
                # Requirement routes are authoritative for structured questions.
                "next_action": "omit",
            }
        ],
    }
    brief = materialize_corpus_brief(
        generation_run_id="run-1",
        project_id="project-1",
        run_corpus_view_id="view-1",
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id="source-map-1",
        source_map=SourceMap(),
        workspace=workspace,
    )

    requests = corpus_brief_acquisition_requests(brief)
    job = _jobs_from_corpus_brief(brief, SourceMap())[0]

    assert len(requests) == 1
    assert requests[0].question_id == "q_parts"
    assert requests[0].reason == (
        "Find the missing primary instrument. The primary instrument is absent from the corpus."
    )
    assert brief.research_handoff[0].question_assignments[0].requirement_ids == ["r_research"]
    assert [item["requirement_id"] for item in job["open_requirements"]] == ["r_research"]


@pytest.mark.asyncio
async def test_orchestrator_routes_passed_triage_acquisition_action_through_existing_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    run = _run(step=V2PipelineStep.EDITORIAL_RESEARCH)
    db = object()
    runtime = object()
    corpus = object()
    pending_state = _triage_state("source_acquisition")
    pending_state.corpus = corpus
    resolved_state = _triage_state("research")
    resolved_state.corpus = corpus
    calls: list[str] = []

    class ReachedResearch(RuntimeError):
        pass

    async def load_show_bible(_db, _run):
        return object()

    async def rounds_used(_db, _run_id):
        return 0

    async def load_corpus(_db, _run):
        return corpus

    async def load_triage(_db, _run):
        return pending_state

    async def acquire(_run, _db, _runtime, **kwargs):
        assert kwargs["triage_state"] is pending_state
        calls.append("source_acquisition")
        return resolved_state, 1

    async def enter_research(_db, _run, _runtime, **kwargs):
        assert kwargs["triage_state"] is resolved_state
        assert kwargs["acquisition_rounds_used"] == 1
        calls.append("editorial_research")
        raise ReachedResearch

    monkeypatch.setattr(orchestrator, "V2Runtime", lambda: runtime)
    monkeypatch.setattr(orchestrator, "_load_show_bible_context", load_show_bible)
    monkeypatch.setattr(orchestrator, "persisted_source_acquisition_rounds_used", rounds_used)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_corpus)
    monkeypatch.setattr(orchestrator, "load_passed_triage_state", load_triage)
    monkeypatch.setattr(orchestrator, "_run_source_acquisition_until_triage_ready", acquire)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", enter_research)

    with pytest.raises(ReachedResearch):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == ["source_acquisition", "editorial_research"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "rounds_used", "message"),
    [
        ("off", 0, "source acquisition is disabled"),
        ("auto", 1, "repair budget is exhausted"),
    ],
)
async def test_pending_acquisition_action_fails_explicitly_when_loop_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    rounds_used: int,
    message: str,
) -> None:
    if mode == "auto":
        monkeypatch.setattr(source_acquisition_flow, "acquisition_enabled", lambda _run: True)

    async def prepare_must_not_run(*_args):
        raise AssertionError("triage must not rerun when acquisition cannot start")

    with pytest.raises(RuntimeError, match=message):
        await source_acquisition_flow.run_source_acquisition_until_triage_ready(
            _run(source_acquisition_mode=mode),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            triage_state=_triage_state("source_acquisition"),  # type: ignore[arg-type]
            trigger_phase="source_triage",
            acquisition_rounds_used=rounds_used,
            prepare_and_triage=prepare_must_not_run,
        )


@pytest.mark.asyncio
async def test_acquisition_round_cannot_finish_while_action_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    pending_state = _triage_state("source_acquisition")
    prepare_calls = 0
    artifact_number = 0

    class FakeDb:
        async def commit(self) -> None:
            return None

    class FakeRuntime:
        def model_for(self, *_args) -> str:
            return "test-model"

        def prompt_hash_for(self, *_args) -> str:
            return "prompt-hash"

        def fallback_metadata_for(self, *_args) -> dict:
            return {}

    class FakeToolset:
        def __init__(self, **_kwargs) -> None:
            pass

        async def restore_unfinished_round(self) -> None:
            return None

        def agent_tools(self) -> list:
            return []

        def round_artifact_payload(self, **_kwargs) -> SourceAcquisitionRound:
            return SourceAcquisitionRound(
                round_number=1,
                trigger_phase="source_triage",
                accepted_count=1,
                ingested_source_ids=["source-new"],
            )

    async def save_artifact(*_args, **_kwargs):
        nonlocal artifact_number
        artifact_number += 1
        return SimpleNamespace(id=f"artifact-{artifact_number}")

    async def start_phase(*_args, **_kwargs) -> None:
        return None

    async def acquire(*_args, **_kwargs) -> SourceAcquisitionCompletion:
        return SourceAcquisitionCompletion(ready=True, summary="One source was added.")

    async def prepare_and_triage(*_args) -> SimpleNamespace:
        nonlocal prepare_calls
        prepare_calls += 1
        return pending_state

    monkeypatch.setattr(source_acquisition_flow, "acquisition_enabled", lambda _run: True)
    monkeypatch.setattr(
        source_acquisition_flow,
        "remaining_acquisition_rounds",
        lambda used: max(0, 1 - used),
    )
    monkeypatch.setattr(source_acquisition_flow, "SourceAcquisitionToolset", FakeToolset)
    monkeypatch.setattr(source_acquisition_flow, "save_artifact", save_artifact)
    monkeypatch.setattr(source_acquisition_flow, "start_phase", start_phase)
    monkeypatch.setattr(source_acquisition_flow, "build_source_acquisition_agent", acquire)

    with pytest.raises(RuntimeError, match="did not resolve the requested CorpusBrief action"):
        await source_acquisition_flow.run_source_acquisition_until_triage_ready(
            run,  # type: ignore[arg-type]
            FakeDb(),  # type: ignore[arg-type]
            FakeRuntime(),  # type: ignore[arg-type]
            triage_state=pending_state,  # type: ignore[arg-type]
            trigger_phase="source_triage",
            acquisition_rounds_used=0,
            prepare_and_triage=prepare_and_triage,
        )

    assert prepare_calls == 1
