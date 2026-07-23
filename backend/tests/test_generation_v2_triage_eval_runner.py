from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.generation_v2.triage_eval_runner import (
    TRIAGE_FIRST_SET_EVAL_MAX_COST_USD,
    _clean_plan_workspace,
    _eval_readiness_report,
    _first_set_structural_report,
    _prepare_eval_resume,
    preflight_first_set_eval,
)
from app.models.generation_run import GenerationStatus
from app.generation_v2.triage_eval_support import (
    EvalBudgetExceeded,
    EvalCostGuard,
    TRIAGE_EVAL_CONTEXT_WINDOW_TOKENS,
    TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS,
    TRIAGE_EVAL_WORST_CASE_CALL_RESERVE_USD,
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

    prepared, prepared_source_run_id = await _prepare_eval_resume(eval_run_id)

    assert prepared is run
    assert str(prepared_source_run_id) == source_run_id
    assert run.status == GenerationStatus.RUNNING
    assert run.error_message is None
    assert run.completed_at is None
    assert workspace["source_assessments"] == [{"source_ref": "S01"}]


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
                "answer_requirements": [{"requirement_id": "q1:r1"}],
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


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        (
            lambda workspace: workspace.update(central_question_id="missing"),
            "central_question_id does not reference a coverage question",
        ),
        (
            lambda workspace: workspace["source_sets"][0].update(
                question_ids=["q1"],
                planned_question_ids=["q1"],
                question_assignments=[{"question_id": "q1", "requirement_ids": ["q1:r1"]}],
            ),
            "coverage questions are unassigned: q2",
        ),
        (
            lambda workspace: workspace["source_sets"][0]["question_assignments"][0].update(
                requirement_ids=[]
            ),
            "coverage requirements are unassigned: q1/q1:r1",
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
                "answer_requirements": [{"requirement_id": "q1:r1"}],
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
        return {"totals": {"call_count": 10, "estimated_token_cost_usd": 1.55}}

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
async def test_eval_cost_guard_reserves_audited_worst_case_call(monkeypatch) -> None:
    async def costs(_db, _run_id):
        return {"totals": {"call_count": 1, "estimated_token_cost_usd": 0.91}}

    monkeypatch.setattr(
        "app.generation_v2.triage_eval_support.summarize_tool_trace_costs",
        costs,
    )

    with pytest.raises(EvalBudgetExceeded, match=r"\$0.88000000 reserved"):
        await EvalCostGuard(max_cost_usd=1.70)(
            db=None,
            generation_run_id="run-1",
            requested_model="gpt-5.4-mini",
            max_output_tokens=None,
            server_compaction_threshold=200_000,
        )


def test_eval_cost_reserve_covers_documented_native_model_limits() -> None:
    documented_worst_case = (
        TRIAGE_EVAL_CONTEXT_WINDOW_TOKENS * 0.75
        + TRIAGE_EVAL_NATIVE_MAX_OUTPUT_TOKENS * 4.50
    ) / 1_000_000

    assert TRIAGE_EVAL_WORST_CASE_CALL_RESERVE_USD >= documented_worst_case


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


def test_first_set_structural_report_requires_grounded_findings_and_current_outcomes() -> None:
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
        "coverage_outcomes": [],
    }
    from app.generation_v2.source_triage_set_state_views import question_workbench_signature

    workspace["coverage_outcomes"] = [
        {
            "set_id": "set-1",
            "question_id": "q1",
            "is_stale": False,
            "workbench_snapshot_id": question_workbench_signature(
                workspace,
                source_set,
                question_id="q1",
            ),
        }
    ]

    report = _first_set_structural_report(workspace, source_set)

    assert report["passed"] is True
