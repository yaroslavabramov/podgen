from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2.triage_eval_runner import (
    HONG_KONG_GOLDEN_MANIFEST_HASH,
    TRIAGE_EVAL_GOLDEN_HONG_KONG,
    TRIAGE_EVAL_STOP_FIRST_SET,
    TRIAGE_EVAL_STOP_FULL_TRIAGE,
    TRIAGE_FIRST_SET_EVAL_MAX_COST_USD,
    _execute_full_triage,
    _clean_plan_workspace,
    _eval_readiness_report,
    _execute_first_set,
    _first_set_structural_report,
    _golden_expectations,
    _golden_source_refs,
    _source_mapping_semantic_expectation_report,
    _prepare_eval_resume,
    preflight_first_set_eval,
)
from app.models.generation_run import GenerationStatus
from app.generation_v2.schemas import SourceTriageCompletion
from app.generation_v2.triage_eval_support import (
    EvalBudgetExceeded,
    EvalCostGuard,
    cancel_and_drain,
)


@pytest.mark.asyncio
async def test_eval_failure_cleanup_drains_an_already_failed_worker() -> None:
    async def fail() -> None:
        raise EvalBudgetExceeded("budget")

    task = asyncio.create_task(fail())
    await asyncio.sleep(0)

    await cancel_and_drain(task)

    assert task.done()


def test_triage_eval_semantic_postflight_checks_mapper_source_analysis() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "set-1", "status": "mapped", "source_refs": ["S03"], "question_ids": ["current"]}
        ],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": "finding:S03:hk10",
                        "statement": "HKMA issued HK$10 notes after public demand persisted.",
                        "question_ids": ["current"],
                        "evidence_ids": ["evidence-hk10"],
                        "limitations": [],
                    }
                ],
                "gaps": [
                    {
                        "question_id": "origin",
                        "description": "The source does not identify the early legal basis.",
                    }
                ],
            }
        ],
    }

    report = _source_mapping_semantic_expectation_report(
        workspace,
        expected_salient_facts=[
            {
                "label": "hk10_public_demand_hkma",
                "source_ref": "S03",
                "question_id": "current",
                "required_terms": ["HK$10", "public demand", "HKMA"],
            }
        ],
        expected_gaps=[
            {
                "label": "early_legal_basis_gap",
                "source_ref": "S03",
                "question_id": "origin",
                "required_terms": ["early legal basis"],
            }
        ],
    )

    assert report["passed"] is True


def test_triage_eval_semantic_postflight_requires_expectations() -> None:
    report = _source_mapping_semantic_expectation_report(
        {"source_sets": [], "source_analyses": []},
        expected_salient_facts=[],
        expected_gaps=[],
    )

    assert report["passed"] is False
    assert "source-mapping semantic postflight requires explicit expectations" in report["issues"]


def test_triage_eval_semantic_postflight_accepts_gap_term_groups() -> None:
    workspace = {
        "source_sets": [{"set_id": "set-1", "status": "mapped", "source_refs": ["S03"]}],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [],
                "gaps": [
                    {
                        "question_id": "q_hkd_current_mechanics",
                        "description": "Источник не раскрывает причину исключения HK$10.",
                    },
                    {
                        "question_id": "q_hkd_origin_private_notes",
                        "description": "Источник не раскрывает юридическую базу частной эмиссии.",
                    },
                ],
            }
        ],
    }

    report = _source_mapping_semantic_expectation_report(
        workspace,
        expected_salient_facts=[],
        expected_gaps=[
            {
                "label": "s03_hk10_specific_exception_gap",
                "source_ref": "S03",
                "question_id": "q_hkd_current_mechanics",
                "required_term_groups": [
                    ["HK$10", "$10"],
                    ["почему", "причин", "объясн", "мотив"],
                ],
            },
            {
                "label": "early_private_note_legal_basis_gap",
                "source_ref": "S03",
                "question_id": "q_hkd_origin_private_notes",
                "required_term_groups": [
                    ["правов", "юрид", "legal"],
                    ["основан", "баз", "basis"],
                ],
            },
        ],
    )

    assert report["passed"] is True


def test_triage_eval_semantic_postflight_checks_research_handoff() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "set-1", "status": "mapped", "source_refs": ["S03"], "question_ids": ["current"]}
        ],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": "finding:S03:hk10",
                        "statement": "HKMA issued HK$10 notes after public demand persisted.",
                        "question_ids": ["current"],
                        "evidence_ids": ["evidence-hk10"],
                        "limitations": [],
                    }
                ],
                "gaps": [],
            }
        ],
    }

    report = _source_mapping_semantic_expectation_report(
        workspace,
        expected_salient_facts=[
            {
                "label": "hk10_public_demand_hkma",
                "source_ref": "S03",
                "question_id": "current",
                "required_terms": ["HK$10", "public demand", "HKMA"],
            }
        ],
        expected_gaps=[],
        research_handoff=[
            {
                "question_ids": ["current"],
                "findings": [
                    {
                        "source_ref": "S03",
                        "statement": "HKMA issued HK$10 notes after public demand persisted.",
                        "evidence_ids": ["evidence-hk10"],
                    }
                ],
            }
        ],
    )

    assert report["passed"] is True
    assert report["handoff_report"]["checked"] is True


def test_hong_kong_golden_expectations_are_scoped_by_eval_mode() -> None:
    first_facts, first_gaps = _golden_expectations(
        TRIAGE_EVAL_GOLDEN_HONG_KONG,
        TRIAGE_EVAL_STOP_FIRST_SET,
    )
    full_facts, full_gaps = _golden_expectations(
        TRIAGE_EVAL_GOLDEN_HONG_KONG,
        TRIAGE_EVAL_STOP_FULL_TRIAGE,
    )

    assert HONG_KONG_GOLDEN_MANIFEST_HASH == (
        "4edb09781e1950b4b2cc3e690975b6af34d24fdd773555565ed5449824559c03"
    )
    assert {item["source_ref"] for item in first_facts + first_gaps} == {"S03"}
    assert {item["source_ref"] for item in full_facts + full_gaps} == {"S01", "S03"}
    assert "S33" not in {item["source_ref"] for item in full_facts + full_gaps}
    assert _golden_source_refs(TRIAGE_EVAL_GOLDEN_HONG_KONG, TRIAGE_EVAL_STOP_FIRST_SET) == ["S03"]
    assert _golden_source_refs(TRIAGE_EVAL_GOLDEN_HONG_KONG, TRIAGE_EVAL_STOP_FULL_TRIAGE) == [
        "S01",
        "S03",
    ]


@pytest.mark.asyncio
async def test_failed_eval_resume_keeps_persisted_set_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_run_id = "10000000-0000-0000-0000-000000000001"
    source_run_id = "20000000-0000-0000-0000-000000000002"
    run = SimpleNamespace(
        id=eval_run_id,
        config={
            "triage_eval": {
                "source_run_id": source_run_id,
                "stop_after": "first_set",
            }
        },
        status=GenerationStatus.FAILED,
        error_message="provider quota",
        completed_at="earlier",
        current_step="triage eval stopped",
    )
    workspace = {
        "source_sets": [{"set_id": "set-1", "status": "in_progress"}],
        "source_assessments": [{"source_ref": "S01"}],
    }

    class Session:
        async def get(self, _model, _run_id):
            return run

        async def commit(self):
            return None

        async def refresh(self, _run):
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    async def load_workspace(_db, _run_id):
        return workspace

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.async_session_factory",
        SessionContext,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_source_triage_workspace",
        load_workspace,
    )

    prepared, prepared_source_run_id = await _prepare_eval_resume(
        eval_run_id,
        expected_stop_after=TRIAGE_EVAL_STOP_FIRST_SET,
    )

    assert prepared is run
    assert str(prepared_source_run_id) == source_run_id
    assert run.status == GenerationStatus.RUNNING
    assert run.error_message is None
    assert run.completed_at is None
    assert workspace["source_assessments"] == [{"source_ref": "S01"}]


@pytest.mark.asyncio
async def test_resume_hydrates_pinned_corpus_without_preparing_a_new_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    source_run_id = uuid.UUID("20000000-0000-0000-0000-000000000002")
    run = SimpleNamespace(
        id=run_id,
        config={
            "triage_eval": {
                "expected_salient_facts": [
                    {
                        "label": "s01_fact",
                        "source_ref": "S01",
                        "question_id": "q1",
                        "required_terms": ["S01 fact"],
                    }
                ],
                "expected_gaps": [],
            }
        },
    )
    workspace = {
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "in_progress",
                "source_refs": ["S01"],
                "question_ids": ["q1"],
            }
        ]
    }
    pinned_corpus = SimpleNamespace(
        run_view=SimpleNamespace(
            manifest_hash="manifest",
            revision=1,
            attempt=1,
        ),
    )
    workspace.update(
        manifest_hash="manifest",
        corpus_revision=1,
        attempt=1,
    )
    captured: dict[str, object] = {}

    class Session:
        async def get(self, _model, _run_id):
            return run

        async def commit(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    class Runner:
        def __init__(self, _run, _db, runtime, *, prepared_corpus=None):
            captured["prepared_corpus"] = prepared_corpus
            self.runtime = runtime

        async def prepare_context(self):
            return SimpleNamespace(
                corpus=captured["prepared_corpus"],
                source_inventory=SimpleNamespace(),
            )

    async def load_workspace(_db, _run_id):
        return workspace

    async def load_pinned_corpus(_db, _run):
        return pinned_corpus

    async def load_source_view(_db, _run_id):
        return SimpleNamespace(
            manifest_hash="manifest",
            revision=1,
            attempt=1,
        )

    async def run_set(**_kwargs):
        workspace["source_sets"][0]["status"] = "mapped"
        workspace["source_analyses"] = [
            {
                "analysis_id": "analysis:S01",
                "source_ref": "S01",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": "finding:S01",
                        "statement": "S01 fact",
                        "question_ids": ["q1"],
                        "evidence_ids": ["evidence:S01"],
                    }
                ],
                "gaps": [],
            }
        ]
        return [SourceTriageCompletion(ready=True, summary="done", notes=[])]

    async def costs(_db, _run_id):
        return {
            "totals": {
                "call_count": 0,
                "estimated_token_cost_usd": 0.0,
                "max_call_cost_usd": 0.0,
            }
        }

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.async_session_factory",
        SessionContext,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_current_v2_corpus",
        load_pinned_corpus,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_current_run_corpus_view",
        load_source_view,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.SourceTriageStageRunner",
        Runner,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.run_source_mapper_for_set",
        run_set,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.summarize_tool_trace_costs",
        costs,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner._first_set_structural_report",
        lambda *_args: {"passed": True, "issues": []},
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner._emit",
        lambda *_args, **_kwargs: None,
    )

    result = await _execute_first_set(
        eval_run_id=run_id,
        source_run_id=source_run_id,
        max_cost_usd=1.70,
        resume_existing=True,
    )

    assert captured["prepared_corpus"] is pinned_corpus
    assert result["set_status"] == "mapped"


@pytest.mark.asyncio
async def test_full_triage_eval_runs_all_sets_then_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    source_run_id = uuid.UUID("20000000-0000-0000-0000-000000000002")
    run = SimpleNamespace(
        id=run_id,
        status=GenerationStatus.RUNNING,
        config={
            "triage_eval": {
                "expected_salient_facts": [
                    {
                        "label": "s01_fact",
                        "source_ref": "S01",
                        "question_id": "q1",
                        "required_terms": ["S01 fact"],
                    }
                ],
                "expected_gaps": [],
            }
        },
    )
    source_workspace = {
        "source_order": ["S01", "S02"],
        "central_question_id": "q1",
        "coverage_questions": [
            {
                "question_id": "q1",
                "importance": "critical",
                "answer_requirements": [{"requirement_id": "q1:r1"}],
            }
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "question_ids": ["q1"],
                "planned_question_ids": ["q1"],
                "question_assignments": [{"question_id": "q1", "requirement_ids": ["q1:r1"]}],
            },
            {
                "set_id": "set-2",
                "source_refs": ["S02"],
                "question_ids": ["q1"],
                "planned_question_ids": ["q1"],
                "question_assignments": [{"question_id": "q1", "requirement_ids": ["q1:r1"]}],
            },
        ],
    }
    fresh_workspace = {
        "source_order": ["S01", "S02"],
        "manifest_hash": "manifest",
        "corpus_revision": 1,
        "attempt": 1,
    }
    state: dict[str, object] = {"workspace": None}
    completed_sets: list[str] = []
    materializer_called = False

    class Session:
        async def get(self, _model, _run_id):
            return run

        async def commit(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    class Runner:
        def __init__(self, _run, _db, runtime, *, prepared_corpus=None):
            self.runtime = runtime

        async def prepare_context(self):
            return SimpleNamespace(
                corpus=SimpleNamespace(
                    run_view=SimpleNamespace(manifest_hash="manifest", revision=1, attempt=1)
                ),
                source_inventory=SimpleNamespace(),
                workspace=fresh_workspace,
            )

        async def materialize_source_mapping(self, _context, workspace):
            nonlocal materializer_called
            materializer_called = True
            return (
                SimpleNamespace(),
                SourceTriageCompletion(ready=True, summary="materialized", notes=[]),
                workspace,
            )

        async def validate_repair_and_persist(self, _context, **kwargs):
            workspace = kwargs["workspace"]
            workspace["status"] = "finalized"
            return SimpleNamespace(
                triage_completion=SourceTriageCompletion(ready=True, summary="done", notes=[]),
                source_map=SimpleNamespace(),
                triage_gate=SimpleNamespace(passed=True),
                corpus_brief=SimpleNamespace(
                    research_handoff=[
                        SimpleNamespace(
                            handoff_id="h1",
                            question_ids=["q1"],
                            findings=[
                                {
                                    "source_ref": "S01",
                                    "statement": "S01 fact",
                                    "evidence_ids": ["evidence:S01"],
                                }
                            ],
                        )
                    ]
                ),
            )

    async def load_workspace(_db, generation_run_id):
        if str(generation_run_id) == str(source_run_id):
            return source_workspace
        return state["workspace"]

    async def save_workspace(_db, *, workspace, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    async def load_source_view(_db, _run_id):
        return SimpleNamespace(manifest_hash="manifest", revision=1, attempt=1)

    async def run_set(**kwargs):
        workspace = kwargs["workspace"]
        source_set = kwargs["source_set"]
        set_id = source_set["set_id"]
        source_ref = source_set["source_refs"][0]
        persisted_set = next(item for item in workspace["source_sets"] if item["set_id"] == set_id)
        persisted_set["status"] = "mapped"
        persisted_set["assessment_source_refs"] = [source_ref]
        finding_id = f"finding:{source_ref}"
        evidence_id = f"evidence:{source_ref}"
        workspace.setdefault("source_assessments", []).append(
            {
                "set_id": set_id,
                "source_ref": source_ref,
                "processing_status": "resolved",
                "representation_status": "readable",
                "finding_ids": [finding_id],
                "evidence_ids": [evidence_id],
            }
        )
        workspace.setdefault("source_findings", []).append(
            {"set_id": set_id, "finding_id": finding_id, "evidence_ids": [evidence_id]}
        )
        workspace.setdefault("source_analyses", []).append(
            {
                "analysis_id": f"analysis:{source_ref}",
                "source_ref": source_ref,
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": finding_id,
                        "statement": f"{source_ref} fact",
                        "question_ids": ["q1"],
                        "evidence_ids": [evidence_id],
                    }
                ],
                "gaps": [],
            }
        )
        completed_sets.append(set_id)
        return [SourceTriageCompletion(ready=True, summary=f"{set_id} done", notes=[])]

    async def costs(_db, _run_id):
        return {
            "totals": {
                "call_count": 0,
                "estimated_token_cost_usd": 0.0,
                "max_call_cost_usd": 0.0,
            }
        }

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.async_session_factory",
        SessionContext,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.SourceTriageStageRunner",
        Runner,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.save_source_triage_workspace",
        save_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_current_run_corpus_view",
        load_source_view,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.run_source_mapper_for_set",
        run_set,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.summarize_tool_trace_costs",
        costs,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner._emit",
        lambda *_args, **_kwargs: None,
    )

    result = await _execute_full_triage(
        eval_run_id=run_id,
        source_run_id=source_run_id,
        max_cost_usd=8.0,
    )

    assert completed_sets == ["set-1", "set-2"]
    assert materializer_called is True
    assert result["structural_report"]["passed"] is True
    assert result["structural_report"]["research_handoff_count"] == 1
    assert run.status == GenerationStatus.COMPLETED


def test_clean_plan_workspace_keeps_plan_and_drops_all_set_execution_memory() -> None:
    fresh = {
        "manifest_hash": "same",
        "source_order": ["S01", "S02"],
        "intent": {"language": "ru"},
        "source_capsules": {"S01": {"title": "A"}},
        "source_assessments": [],
    }
    source = {
        "status": "planned",
        "central_question_id": "q1",
        "plan_summary": "План",
        "coverage_questions": [{"question_id": "q1", "question": "Почему?"}],
        "source_sets": [
            {
                "set_id": "set-1",
                "label": "Первая очередь",
                "status": "in_progress",
                "source_refs": ["S01", "S02"],
                "question_ids": ["q1", "q-runtime"],
                "planned_question_ids": ["q1"],
                "cross_question_ids": ["q-runtime"],
                "question_assignments": [{"question_id": "q1", "requirement_ids": []}],
                "evidence_ids": ["old-evidence"],
                "pending_exact_reads": [{"source_ref": "S01"}],
                "findings_summary": "Старый частичный результат",
            }
        ],
        "source_findings": [{"finding_id": "old-finding"}],
        "source_read_receipts": [{"receipt_ref": "old-receipt"}],
    }

    clean = _clean_plan_workspace(fresh_workspace=fresh, source_workspace=source)

    assert clean["status"] == "planned"
    assert clean["coverage_questions"] == source["coverage_questions"]
    assert clean["source_sets"] == [
        {
            "set_id": "set-1",
            "label": "Первая очередь",
            "source_refs": ["S01", "S02"],
            "question_ids": ["q1"],
            "assigned_question_ids": ["q1"],
            "planned_question_ids": ["q1"],
            "cross_question_ids": [],
            "question_assignments": [{"question_id": "q1", "requirement_ids": []}],
            "status": "planned",
        }
    ]
    assert clean["source_findings"] == []
    assert clean["source_read_receipts"] == []
    assert clean["source_assessments"] == []
    assert clean["source_capsules"] == fresh["source_capsules"]


def test_eval_preflight_ignores_runtime_cross_question_discoveries() -> None:
    workspace = {
        "source_order": ["S01", "S02"],
        "central_question_id": "q1",
        "coverage_questions": [
            {
                "question_id": "q1",
                "importance": "critical",
                "answer_requirements": [{"requirement_id": "q1:r1", "synthesis_only": True}],
            },
            {
                "question_id": "q2",
                "importance": "supporting",
                "answer_requirements": [{"requirement_id": "q2:r1"}],
            },
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": ["q1", "q2"],
                "planned_question_ids": ["q1"],
                "cross_question_ids": ["q2"],
                "question_assignments": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"]}
                ],
            },
            {
                "set_id": "set-2",
                "set_kind": "coverage",
                "source_refs": ["S02"],
                "question_ids": ["q2"],
                "planned_question_ids": ["q2"],
                "cross_question_ids": [],
                "question_assignments": [
                    {"question_id": "q2", "requirement_ids": ["q2:r1"]}
                ],
            },
        ],
    }

    report = _eval_readiness_report(source=SimpleNamespace(id="run-1"), workspace=workspace)

    assert report["ready"] is True
    assert report["issues"] == []


def test_clean_plan_workspace_rejects_missing_or_duplicate_source_assignment() -> None:
    fresh = {"source_order": ["S01", "S02"]}
    source = {
        "central_question_id": "q1",
        "coverage_questions": [{"question_id": "q1"}],
        "source_sets": [
            {"set_id": "one", "source_refs": ["S01"]},
            {"set_id": "two", "source_refs": ["S01"]},
        ],
    }

    with pytest.raises(RuntimeError, match="exactly once"):
        _clean_plan_workspace(fresh_workspace=fresh, source_workspace=source)


def test_eval_preflight_is_read_only_and_rejects_incomplete_question_contracts() -> None:
    report = _eval_readiness_report(
        source=SimpleNamespace(id="run-1"),
        workspace={
            "source_order": ["S01"],
            "central_question_id": "q1",
            "coverage_questions": [
                {
                    "question_id": "q1",
                    "importance": "critical",
                    "answer_requirements": [],
                }
            ],
            "source_sets": [
                {
                    "set_id": "set-1",
                    "source_refs": ["S01"],
                    "question_ids": ["q1"],
                    "planned_question_ids": ["q1"],
                    "question_assignments": [{"question_id": "q1", "requirement_ids": []}],
                }
            ],
        },
    )

    assert report["ready"] is False
    assert report["issues"] == ["coverage question q1 has no answer requirements"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda workspace: workspace["coverage_questions"][0].update(
            importance="supporting"
        ),
        lambda workspace: workspace["source_sets"][0].update(set_kind="bogus"),
    ],
)
def test_eval_preflight_uses_the_product_plan_contract(mutation) -> None:
    workspace = {
        "source_order": ["S01"],
        "central_question_id": "q1",
        "coverage_questions": [
            {
                "question_id": "q1",
                "importance": "critical",
                "answer_requirements": [{"requirement_id": "q1:r1"}],
            }
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": ["q1"],
                "planned_question_ids": ["q1"],
                "question_assignments": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"]}
                ],
            }
        ],
    }
    mutation(workspace)

    report = _eval_readiness_report(source=SimpleNamespace(id="run-1"), workspace=workspace)

    assert report["ready"] is False


@pytest.mark.asyncio
async def test_eval_preflight_rejects_changed_corpus_before_cloning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id="run-1", project_id="project-1", config={})
    workspace = {
        "source_order": ["S01"],
        "central_question_id": "q1",
        "coverage_questions": [
            {
                "question_id": "q1",
                "importance": "critical",
                "answer_requirements": [{"requirement_id": "q1:r1"}],
            }
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": ["q1"],
                "planned_question_ids": ["q1"],
                "question_assignments": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"]}
                ],
            }
        ],
    }

    class Session:
        async def get(self, _model, _run_id):
            return source

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    async def load_workspace(_db, _run_id):
        return workspace

    async def load_view(_db, _run_id):
        return SimpleNamespace(manifest_hash="old")

    async def preview_manifest(_db, _source):
        return "new"

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.async_session_factory",
        SessionContext,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_current_run_corpus_view",
        load_view,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.preview_v2_corpus_manifest_hash",
        preview_manifest,
    )

    report = await preflight_first_set_eval("run-1")

    assert report["ready"] is False
    assert report["issues"][-1] == "current project corpus differs from the source run"


@pytest.mark.asyncio
async def test_eval_preflight_rejects_wrong_golden_manifest_before_cloning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _golden_preflight_workspace()
    _install_preflight_mocks(monkeypatch, workspace=workspace, manifest_hash=HONG_KONG_GOLDEN_MANIFEST_HASH)

    report = await preflight_first_set_eval(
        uuid.UUID("10000000-0000-0000-0000-000000000001"),
        mode=TRIAGE_EVAL_STOP_FIRST_SET,
        expected_manifest_hash="wrong",
        expected_source_refs=["S03"],
        expected_salient_facts=[
            {
                "label": "s03_fact",
                "source_ref": "S03",
                "question_id": "q_hkd_current_mechanics",
                "required_terms": ["HK$10"],
            }
        ],
        expected_gaps=[],
    )

    assert report["ready"] is False
    assert report["issues"][-1].startswith("golden corpus manifest mismatch")


@pytest.mark.asyncio
async def test_eval_preflight_rejects_first_set_golden_source_outside_first_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _golden_preflight_workspace()
    _install_preflight_mocks(monkeypatch, workspace=workspace, manifest_hash=HONG_KONG_GOLDEN_MANIFEST_HASH)

    report = await preflight_first_set_eval(
        uuid.UUID("10000000-0000-0000-0000-000000000001"),
        mode=TRIAGE_EVAL_STOP_FIRST_SET,
        expected_manifest_hash=HONG_KONG_GOLDEN_MANIFEST_HASH,
        expected_source_refs=["S01", "S03"],
        expected_salient_facts=[
            {
                "label": "s01_fact",
                "source_ref": "S01",
                "question_id": "q_hkd_current_mechanics",
                "required_terms": ["HK$10"],
            }
        ],
        expected_gaps=[],
    )

    assert report["ready"] is False
    assert report["issues"][-1] == (
        "first_set golden expectations reference sources outside first set: S01"
    )


@pytest.mark.asyncio
async def test_eval_preflight_rejects_custom_expectation_unknown_source_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _golden_preflight_workspace()
    _install_preflight_mocks(monkeypatch, workspace=workspace, manifest_hash=HONG_KONG_GOLDEN_MANIFEST_HASH)

    report = await preflight_first_set_eval(
        uuid.UUID("10000000-0000-0000-0000-000000000001"),
        mode=TRIAGE_EVAL_STOP_FULL_TRIAGE,
        expected_manifest_hash=HONG_KONG_GOLDEN_MANIFEST_HASH,
        expected_source_refs=[],
        expected_salient_facts=[
            {
                "label": "custom_s99",
                "source_ref": "S99",
                "question_id": "q_hkd_current_mechanics",
                "required_terms": ["anything"],
            }
        ],
        expected_gaps=[],
    )

    assert report["ready"] is False
    assert report["issues"][-1] == "golden corpus missing source_refs: S99"


def _golden_preflight_workspace() -> dict:
    return {
        "source_order": ["S01", "S03"],
        "central_question_id": "q_root",
        "coverage_questions": [
            {
                "question_id": "q_root",
                "importance": "critical",
                "answer_requirements": [{"requirement_id": "q_root:r1"}],
            },
            {
                "question_id": "q_hkd_current_mechanics",
                "parent_question_id": "q_root",
                "answer_requirements": [{"requirement_id": "q_hkd_current_mechanics:r1"}],
            },
            {
                "question_id": "q_hkd_origin_private_notes",
                "parent_question_id": "q_root",
                "answer_requirements": [{"requirement_id": "q_hkd_origin_private_notes:r1"}],
            },
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "set_kind": "coverage",
                "source_refs": ["S03"],
                "question_ids": [],
                "planned_question_ids": [],
                "question_assignments": [],
            },
            {
                "set_id": "set-2",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": [],
                "planned_question_ids": [],
                "question_assignments": [],
            },
        ],
    }


def _install_preflight_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: dict,
    manifest_hash: str,
) -> None:
    source = SimpleNamespace(id=uuid.UUID("10000000-0000-0000-0000-000000000001"), project_id="project-1", config={})

    class Session:
        async def get(self, _model, _run_id):
            return source

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    async def load_workspace(_db, _run_id):
        return workspace

    async def load_view(_db, _run_id):
        return SimpleNamespace(manifest_hash=manifest_hash)

    async def preview_manifest(_db, _source):
        return manifest_hash

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.async_session_factory",
        SessionContext,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_source_triage_workspace",
        load_workspace,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.load_current_run_corpus_view",
        load_view,
    )
    monkeypatch.setattr(
        "app.generation_v2.triage_eval_runner.preview_v2_corpus_manifest_hash",
        preview_manifest,
    )


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        (
            lambda workspace: workspace.update(central_question_id="missing"),
            "central_question_id does not reference a coverage question",
        ),
        (
            lambda workspace: workspace["source_sets"][0].update(
                question_ids=["missing"],
                planned_question_ids=["missing"],
                question_assignments=[{"question_id": "missing", "requirement_ids": []}],
            ),
            "source sets reference unknown questions: missing",
        ),
    ],
)
def test_eval_preflight_rejects_incomplete_plan_assignments(mutation, expected_issue: str) -> None:
    workspace = {
        "source_order": ["S01"],
        "central_question_id": "q1",
        "coverage_questions": [
            {
                "question_id": "q1",
                "importance": "critical",
                "answer_requirements": [{"requirement_id": "q1:r1", "synthesis_only": True}],
            },
            {
                "question_id": "q2",
                "answer_requirements": [{"requirement_id": "q2:r1"}],
            },
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "question_ids": ["q1", "q2"],
                "planned_question_ids": ["q1", "q2"],
                "question_assignments": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"]},
                    {"question_id": "q2", "requirement_ids": ["q2:r1"]},
                ],
            }
        ],
    }
    mutation(workspace)

    report = _eval_readiness_report(source=SimpleNamespace(id="run-1"), workspace=workspace)

    assert report["ready"] is False
    assert expected_issue in report["issues"]


def test_eval_cost_ceiling_is_immutable() -> None:
    assert TRIAGE_FIRST_SET_EVAL_MAX_COST_USD == 1.70


@pytest.mark.asyncio
async def test_eval_cost_guard_stops_before_an_unreserved_provider_call(monkeypatch) -> None:
    async def costs(_db, _run_id):
        return {
            "totals": {
                "call_count": 10,
                "estimated_token_cost_usd": 1.68,
                "max_call_cost_usd": 0.03,
            }
        }

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_support.summarize_tool_trace_costs",
        costs,
    )

    with pytest.raises(EvalBudgetExceeded, match="stopped before the next provider call"):
        await EvalCostGuard(max_cost_usd=1.70)(
            db=None,
            generation_run_id="run-1",
            requested_model="gpt-5.4-mini",
            max_output_tokens=None,
            server_compaction_threshold=200_000,
        )


@pytest.mark.asyncio
async def test_eval_cost_guard_reserves_largest_observed_call(monkeypatch) -> None:
    async def costs(_db, _run_id):
        return {
            "totals": {
                "call_count": 10,
                "estimated_token_cost_usd": 1.65,
                "max_call_cost_usd": 0.06,
            }
        }

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_support.summarize_tool_trace_costs",
        costs,
    )

    with pytest.raises(EvalBudgetExceeded, match=r"\$0.06000000 observed-call reserve"):
        await EvalCostGuard(max_cost_usd=1.70)(
            db=None,
            generation_run_id="run-1",
            requested_model="gpt-5.4-mini",
            max_output_tokens=None,
            server_compaction_threshold=200_000,
        )


@pytest.mark.asyncio
async def test_eval_cost_guard_allows_headroom_above_observed_call(monkeypatch) -> None:
    async def costs(_db, _run_id):
        return {
            "totals": {
                "call_count": 64,
                "estimated_token_cost_usd": 0.84,
                "max_call_cost_usd": 0.04,
            }
        }

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_support.summarize_tool_trace_costs",
        costs,
    )

    await EvalCostGuard(max_cost_usd=1.70)(
        db=None,
        generation_run_id="run-1",
        requested_model="gpt-5.4-mini",
        max_output_tokens=None,
        server_compaction_threshold=200_000,
    )


@pytest.mark.asyncio
async def test_eval_cost_guard_fails_closed_for_an_unaudited_model(monkeypatch) -> None:
    async def costs(_db, _run_id):
        raise AssertionError("Unsupported model must be rejected before reading spend")

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_support.summarize_tool_trace_costs",
        costs,
    )

    with pytest.raises(EvalBudgetExceeded, match="supports only gpt-5.4-mini"):
        await EvalCostGuard(max_cost_usd=1.70)(
            db=None,
            generation_run_id="run-1",
            requested_model="gpt-5.4",
            max_output_tokens=None,
            server_compaction_threshold=200_000,
        )


@pytest.mark.asyncio
async def test_eval_cost_guard_rejects_unknown_future_mini_slug(monkeypatch) -> None:
    async def costs(_db, _run_id):
        raise AssertionError("Unsupported model must be rejected before reading spend")

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_support.summarize_tool_trace_costs",
        costs,
    )

    with pytest.raises(EvalBudgetExceeded, match="supports only gpt-5.4-mini"):
        await EvalCostGuard(max_cost_usd=1.70)(
            db=None,
            generation_run_id="run-1",
            requested_model="gpt-5.4-mini-future",
            max_output_tokens=None,
            server_compaction_threshold=200_000,
        )


def test_first_set_structural_report_requires_grounded_findings_without_set_outcomes() -> None:
    source_set = {"set_id": "set-1", "source_refs": ["S01"], "question_ids": ["q1"]}
    workspace = {
        "coverage_questions": [
            {
                "question_id": "q1",
                "answer_requirements": [{"requirement_id": "q1:r1"}],
            }
        ],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "finding_ids": ["finding-1"],
                "no_findings_reason": "",
            }
        ],
        "source_findings": [
            {"set_id": "set-1", "finding_id": "finding-1", "evidence_ids": ["evidence-1"]}
        ],
        "coverage_hints": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "question_id": "q1",
                "candidate_finding_ids": ["finding-1"],
                "candidate_evidence_ids": ["evidence-1"],
            }
        ],
        "coverage_outcomes": [],
    }

    report = _first_set_structural_report(workspace, source_set)

    assert report["passed"] is True
    assert report["coverage_hint_count"] == 1
    assert report["legacy_outcome_count"] == 0
