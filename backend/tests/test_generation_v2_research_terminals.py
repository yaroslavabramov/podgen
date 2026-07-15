from __future__ import annotations

import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.generation_v2 import research_workspace as workspace_api
from app.generation_v2.agents import research_memo_agent
from app.generation_v2.research_workspace import (
    _merge_corpus_handoff_jobs,
    build_research_workspace_tools,
)
from app.generation_v2.research_workspace_prompt_payloads import current_job_prompt_payload
from app.generation_v2.research_workspace_repair_budget import (
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    TARGETED_REPAIR_BUDGET,
)
from app.generation_v2.prompting.research import RESEARCH_AUDIT_PROMPT
from app.generation_v2.prompting.system import RESEARCH_AUDIT_SYSTEM
from app.generation_v2.research_workspace_planner_tools import research_planner_tool_handlers
from app.generation_v2.schemas import ResearchWorkflowCompletion, SourceMap
from app.generation_v2.tools.evidence_tool_handler_actions import read_evidence_tool_action


def _brief(*question_ids: str) -> dict:
    return {
        "coverage_questions": [
            {
                "question_id": question_id,
                "question": f"Question {question_id}?",
            }
            for question_id in question_ids
        ],
        "research_handoff": [
            {
                "handoff_id": "handoff_main",
                "question_ids": list(question_ids),
            }
        ],
    }


@pytest.mark.asyncio
async def test_research_terminal_tools_resume_from_durable_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "status": "planned",
        "plan_summary": "Durable plan.",
        "source_order": [],
        "investigation_jobs": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)

    planner_tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="planner",
    )
    planner_finish = next(tool for tool in planner_tools if tool.name == "finalize_research_plan")
    assert planner_finish.terminal is True
    planner_result = await planner_finish.terminal_resume_loader()  # type: ignore[misc]
    assert planner_result is not None
    assert planner_result.completion == ResearchWorkflowCompletion(
        ready=True,
        summary="Durable plan.",
        notes=[],
    )

    workspace.update(
        {
            "status": "planned",
            "investigation_jobs": [
                {
                    "job_id": "job_1",
                    "status": "deferred",
                    "summary": "Question was investigated.",
                    "deferred_reason": "The corpus does not answer it.",
                }
            ],
        }
    )
    job_tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="job",
        current_job_id="job_1",
    )
    job_finish = next(tool for tool in job_tools if tool.name == "finish_research_job")
    assert job_finish.terminal is True
    job_result = await job_finish.terminal_resume_loader()  # type: ignore[misc]
    assert job_result is not None
    assert job_result.completion.notes == ["The corpus does not answer it."]

    workspace.update(
        {
            "status": "finalized",
            "memo_fields": {"title": "Durable memo"},
        }
    )
    finalizer_tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="finalizer",
    )
    memo_finish = next(tool for tool in finalizer_tools if tool.name == "finalize_research_memo")
    assert memo_finish.terminal is True
    memo_result = await memo_finish.terminal_resume_loader()  # type: ignore[misc]
    assert memo_result is not None
    assert memo_result.completion.summary == "Durable memo"


@pytest.mark.asyncio
async def test_planner_preserves_authoritative_handoff_question_when_refining_seeded_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "status": "planning",
        "source_order": ["S01"],
        "corpus_brief": _brief("q_main"),
        "investigation_jobs": [
            {
                "job_id": "brief_01",
                "corpus_handoff_id": "handoff_main",
                "coverage_question_ids": ["q_main"],
                "source_refs": ["S01"],
                "questions": ["Question q_main?"],
                "starting_evidence_ids": ["evidence_seed"],
                "evidence_ids": [],
                "status": "planned",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, generation_run_id, workspace: dict, status=None):
        del generation_run_id, status
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    upsert, finalize = research_planner_tool_handlers(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
    )

    result = await upsert(
        {
            "job_id": "brief_01",
            "label": "Refined investigation",
            "story_job": "mechanism",
            "purpose": "Resolve the handoff question.",
            "source_refs": [],
            "questions": ["A narrower follow-up question?"],
            "required_atom_kinds": ["mechanism"],
            "priority": "high",
            "allow_broad_search": False,
        }
    )

    assert result["accepted"] is True
    job = workspace["investigation_jobs"][0]
    assert job["corpus_handoff_id"] == "handoff_main"
    assert job["coverage_question_ids"] == ["q_main"]
    assert job["source_refs"] == ["S01"]
    assert job["questions"] == ["Question q_main?", "A narrower follow-up question?"]
    assert job["starting_evidence_ids"] == ["evidence_seed"]
    assert job["evidence_ids"] == []
    assert (await finalize({"summary": "Plan preserves the handoff."}))["accepted"] is True


def test_new_corpus_handoff_is_merged_into_resumed_repair_jobs() -> None:
    resumed = [
        {
            "job_id": "repair_old",
            "corpus_handoff_id": "handoff_old",
            "coverage_question_ids": ["q_old"],
            "source_refs": ["S01"],
            "questions": ["Old repair question?"],
            "evidence_ids": [],
        }
    ]
    seeded = [
        {
            "job_id": "brief_01",
            "corpus_handoff_id": "handoff_new",
            "coverage_question_ids": ["q_new"],
            "source_refs": ["S02"],
            "questions": ["New corpus question?"],
            "starting_evidence_ids": ["current-evidence"],
            "evidence_ids": [],
        }
    ]

    jobs = _merge_corpus_handoff_jobs(resumed, seeded)

    assert [job["corpus_handoff_id"] for job in jobs] == ["handoff_new", "handoff_old"]
    assert jobs[0]["starting_evidence_ids"] == ["current-evidence"]
    assert jobs[0]["evidence_ids"] == []


def test_job_prompt_exposes_all_starting_evidence_through_preview_and_paging() -> None:
    evidence_ids = [f"evidence-{index}" for index in range(15)]
    payload = current_job_prompt_payload(
        {
            "job_id": "job_1",
            "questions": ["What happened?"],
            "observations": [],
            "starting_evidence_ids": evidence_ids,
            "evidence_ids": [],
        }
    )

    assert payload["starting_evidence_ids"] == evidence_ids[:12]
    assert payload["starting_evidence_count"] == 15
    assert payload["pages"]["starting_evidence"]["omitted_count"] == 3
    assert payload["pages"]["starting_evidence"]["next_cursor"] == "12"


def test_research_audit_does_not_enforce_duration_or_story_taxonomy_quotas() -> None:
    assert "Do not demand a minimum atom count" in RESEARCH_AUDIT_PROMPT
    assert "Do not demand a fixed number of findings" in RESEARCH_AUDIT_SYSTEM
    assert "For 45+ minute work" not in RESEARCH_AUDIT_PROMPT
    assert "missing context/mechanism/change/tension/impact" not in RESEARCH_AUDIT_SYSTEM


@pytest.mark.asyncio
async def test_starting_evidence_must_be_read_before_research_can_promote_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "current_job": {"job_id": "job_1"},
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "source_refs": ["S01"],
                "allow_broad_search": False,
                "questions": ["What happened?"],
                "status": "in_progress",
                "observations": [],
                "atom_ids": [],
                "starting_evidence_ids": [evidence_id],
                "evidence_ids": [],
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **_kwargs):
        return SimpleNamespace(payload=workspace)

    class EvidenceTools:
        _research_read_evidence_ids: set[str] = set()

        async def _evidence_in_run(self, requested_id: str):
            assert requested_id == evidence_id
            return SimpleNamespace(source_id="source-1")

        async def _source_ref_for_id(self, _source_id: str):
            return "S01"

    evidence_tools = EvidenceTools()
    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        evidence_toolset=evidence_tools,
        mode="job",
        current_job_id="job_1",
    )
    observe = next(tool for tool in tools if tool.name == "record_research_observation")
    arguments = {
        "job_id": "job_1",
        "source_refs": ["S01"],
        "evidence_ids": [evidence_id],
        "note": "The seed supports the answer.",
        "pending_questions": [],
    }

    rejected = await observe.handler(arguments)
    evidence_tools._research_read_evidence_ids = {evidence_id}
    accepted = await observe.handler(arguments)

    assert rejected["accepted"] is False
    assert rejected["invalid_evidence_ids"] == [evidence_id]
    assert accepted["accepted"] is True
    assert workspace["investigation_jobs"][0]["evidence_ids"] == [evidence_id]


@pytest.mark.asyncio
async def test_successful_research_read_marks_seed_as_inspected() -> None:
    evidence_id = uuid.uuid4()

    class EvidenceTools:
        research_current_job_id = "job_1"

        async def _triage_read_guard_error(self):
            return None

        async def _evidence_in_run(self, requested_id: str):
            assert requested_id == str(evidence_id)
            return SimpleNamespace(source_id="source-1")

        async def _triage_scope_error_for_source_ids(self, _source_ids: list[str]):
            return None

        async def _evidence_detail_payload(self, _item, *, max_snippet_chars: int):
            assert max_snippet_chars == 2400
            return {"evidence_id": str(evidence_id), "snippet": "Grounded text."}

    evidence_tools = EvidenceTools()

    result = await read_evidence_tool_action(evidence_tools, {"evidence_id": str(evidence_id)})

    assert result["found"] is True
    assert evidence_tools._research_read_evidence_ids == {str(evidence_id)}


@pytest.mark.asyncio
async def test_planner_rejects_only_concretely_unassigned_handoff_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "status": "planning",
        "source_order": [],
        "corpus_brief": _brief("q_kept", "q_missing"),
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "coverage_question_ids": ["q_kept"],
                "questions": ["Question q_kept?"],
                "status": "planned",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    _upsert, finalize = research_planner_tool_handlers(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
    )

    result = await finalize({"summary": "Incomplete plan."})

    assert result == {
        "accepted": False,
        "error": "Every CorpusBrief handoff question must remain assigned to a research job",
        "missing_coverage_question_ids": ["q_missing"],
        "missing_questions": ["Question q_missing?"],
    }


@pytest.mark.asyncio
async def test_handoff_assignment_uses_stable_question_id_not_exact_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "status": "planning",
        "source_order": [],
        "corpus_brief": _brief("q_main"),
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "coverage_question_ids": ["q_main"],
                "questions": ["A clearer reformulation of the same investigation?"],
                "status": "planned",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    _upsert, finalize = research_planner_tool_handlers(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
    )

    assert (await finalize({"summary": "Stable handoff retained."}))["accepted"] is True


@pytest.mark.asyncio
async def test_finalized_research_workspace_is_returned_without_destructive_refinalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"},
    )
    workspace = {
        "status": "finalized",
        "source_order": [],
        "memo_fields": {
            "title": "Existing memo",
            "thesis": "Existing thesis.",
            "research_synthesis": "Existing synthesis.",
            "source_coverage": [],
        },
        "atom_drafts": [
            {
                "atom_id": "atom_1",
                "kind": "mechanism",
                "title": "Mechanism",
                "claim": "Supported claim.",
                "explanation": "Supported explanation.",
                "story_value": "Useful in treatment.",
                "evidence_ids": ["evidence_1"],
            }
        ],
        "investigation_jobs": [],
        "repair_budget": {},
        "repair_operations": {},
    }

    async def ensure(*_args, **_kwargs):
        return deepcopy(workspace)

    async def prepare(_db, *, run, workspace, save_workspace):
        del run, save_workspace
        return workspace, []

    async def must_not_run(**_kwargs):
        raise AssertionError("A finalized durable memo must not be cleared and rebuilt")

    monkeypatch.setattr(research_memo_agent, "ensure_research_workspace", ensure)
    monkeypatch.setattr(research_memo_agent, "prepare_research_jobs_for_resume", prepare)
    monkeypatch.setattr(research_memo_agent, "build_research_finalizer_agent", must_not_run)

    memo = await research_memo_agent.build_research_memo(
        SimpleNamespace(),  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        source_cards=[],
        quote_cards=[],
        source_map=SourceMap(),
        source_inventory=SourceMap(),
        run_planner=False,
    )

    assert memo.title == "Existing memo"
    assert memo.thesis == "Existing thesis."


@pytest.mark.asyncio
async def test_accepted_repair_finalizer_is_not_rebuilt_after_bookkeeping_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"},
    )
    workspace = {
        "status": "finalized",
        "source_order": [],
        "memo_fields": {
            "title": "Repaired memo",
            "thesis": "Repaired thesis.",
            "research_synthesis": "Repaired synthesis.",
            "source_coverage": [],
            "risk_register": [],
        },
        "atom_drafts": [
            {
                "atom_id": "atom_1",
                "kind": "mechanism",
                "title": "Mechanism",
                "claim": "Supported claim.",
                "explanation": "Supported explanation.",
                "story_value": "Useful in treatment.",
                "evidence_ids": ["evidence_1"],
            }
        ],
        "investigation_jobs": [],
        "repair_budget": {},
        "repair_operations": {
            TARGETED_REPAIR_BUDGET: {
                "kind": "research_jobs",
                "status": REPAIR_IN_PROGRESS,
                "payload": {},
            }
        },
    }
    saved: list[dict] = []

    async def ensure(*_args, **_kwargs):
        return deepcopy(workspace)

    async def prepare(_db, *, run, workspace, save_workspace):
        del run, save_workspace
        return workspace, []

    async def save(_db, *, generation_run_id, workspace: dict, status=None):
        del generation_run_id, status
        saved.append(deepcopy(workspace))
        return SimpleNamespace(payload=workspace)

    async def must_not_run(**_kwargs):
        raise AssertionError("Accepted repair memo must not be rebuilt")

    class Database:
        async def commit(self):
            return None

    monkeypatch.setattr(research_memo_agent, "ensure_research_workspace", ensure)
    monkeypatch.setattr(research_memo_agent, "prepare_research_jobs_for_resume", prepare)
    monkeypatch.setattr(research_memo_agent, "save_research_workspace", save)
    monkeypatch.setattr(research_memo_agent, "build_research_finalizer_agent", must_not_run)

    memo = await research_memo_agent.build_research_memo(
        Database(),  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        source_cards=[],
        quote_cards=[],
        source_map=SourceMap(),
        source_inventory=SourceMap(),
        run_planner=False,
    )

    assert memo.title == "Repaired memo"
    assert saved[-1]["repair_operations"][TARGETED_REPAIR_BUDGET]["status"] == REPAIR_COMPLETED
