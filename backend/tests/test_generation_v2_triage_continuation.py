from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.source_agents import build_source_triage_set_agent
from app.generation_v2.pipeline import source_triage_set_flow, source_triage_stage
from app.generation_v2.pipeline.source_triage_set_flow import SourceTriageSetOperationalError
from app.generation_v2.runtime import AgentTerminalNotReached
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion


class _EvidenceToolset:
    def __init__(self, **_kwargs) -> None:
        pass

    def agent_tools(self) -> list[object]:
        return []

    async def persist_hosted_file_search_calls(self, *_args, **_kwargs) -> None:
        return None


def _workspace() -> dict:
    return {
        "status": "planned",
        "intent": {"language": "en"},
        "source_order": ["S01"],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "planned",
                "source_refs": ["S01"],
                "question_ids": ["q-1"],
            }
        ],
        "coverage_questions": [{"question_id": "q-1", "question": "Why?"}],
        "source_assessments": [],
        "coverage_outcomes": [],
    }


def _set_status(workspace: dict, status: str) -> None:
    workspace["source_sets"][0]["status"] = status
    workspace["current_set"] = None if status in {"mapped", "deferred", "needs_repair"} else {
        "set_id": "set-1"
    }


async def _noop_stop_check() -> None:
    return None


@pytest.mark.asyncio
async def test_unfinished_set_reactivates_with_saved_semantic_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _workspace()
    activations: list[dict] = []
    unfinished_events: list[dict] = []

    async def start(_db, **_kwargs):
        _set_status(stored, "in_progress")
        return deepcopy(stored)

    async def load(_db, _run_id):
        return deepcopy(stored)

    async def run_agent(_db, _run, _runtime, *, source_set_payload, **_kwargs):
        activations.append(deepcopy(source_set_payload))
        if len(activations) == 1:
            stored["source_assessments"] = [
                {
                    "set_id": "set-1",
                    "source_ref": "S01",
                    "source_id": "source-1",
                    "processing_status": "resolved",
                    "findings": ["A durable finding"],
                }
            ]
            raise AgentTerminalNotReached("The agent returned before its terminal tool.")
        else:
            _set_status(stored, "mapped")
            stored["source_sets"][0]["findings_summary"] = "Mapped after continuation."
        return SourceTriageCompletion(ready=True, summary=f"activation-{len(activations)}", notes=[])

    async def record_unfinished(**kwargs):
        unfinished_events.append(kwargs)

    monkeypatch.setattr(source_triage_set_flow, "EvidenceToolset", _EvidenceToolset)
    monkeypatch.setattr(source_triage_set_flow, "start_triage_source_set", start)
    monkeypatch.setattr(source_triage_set_flow, "load_source_triage_workspace", load)
    monkeypatch.setattr(source_triage_set_flow, "build_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(source_triage_set_flow, "build_source_triage_set_agent", run_agent)
    monkeypatch.setattr(source_triage_set_flow, "mark_source_triage_set_unfinished", record_unfinished)
    monkeypatch.setattr(source_triage_set_flow, "assert_step_not_stopped", _noop_stop_check)

    completion = await source_triage_set_flow.run_source_triage_set(
        run=SimpleNamespace(id="run-1"),
        db=object(),  # type: ignore[arg-type]
        runtime=object(),
        corpus=SimpleNamespace(run_view=object(), provider_index=None, vector_store_id=None),
        source_inventory=SourceMap(),
        workspace=deepcopy(stored),
        source_set=deepcopy(stored["source_sets"][0]),
    )

    assert completion.summary == "activation-2"
    assert len(activations) == 2
    assert activations[1]["set"]["source_assessment_count"] == 1
    assert unfinished_events[0]["progress_saved"] is True
    assert unfinished_events[0]["stalled"] is False
    assert stored["source_sets"][0]["status"] == "mapped"


@pytest.mark.asyncio
async def test_worker_recovery_keeps_existing_in_progress_set_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _workspace()
    _set_status(stored, "in_progress")
    stored["current_set"] = {"set_id": "set-1", "started_at": "durable-start"}
    stored["source_sets"][0]["started_at"] = "durable-start"
    stored["source_assessments"] = [
        {"set_id": "set-1", "source_ref": "S01", "processing_status": "resolved"}
    ]
    seen_payloads: list[dict] = []

    async def start_must_not_run(*_args, **_kwargs):
        raise AssertionError("An in-progress set must resume without resetting its durable start state")

    async def load(_db, _run_id):
        return deepcopy(stored)

    async def run_agent(_db, _run, _runtime, *, source_set_payload, **_kwargs):
        seen_payloads.append(deepcopy(source_set_payload))
        _set_status(stored, "mapped")
        return SourceTriageCompletion(ready=True, summary="Recovered and mapped.", notes=[])

    monkeypatch.setattr(source_triage_set_flow, "EvidenceToolset", _EvidenceToolset)
    monkeypatch.setattr(source_triage_set_flow, "start_triage_source_set", start_must_not_run)
    monkeypatch.setattr(source_triage_set_flow, "load_source_triage_workspace", load)
    monkeypatch.setattr(source_triage_set_flow, "build_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(source_triage_set_flow, "build_source_triage_set_agent", run_agent)
    monkeypatch.setattr(source_triage_set_flow, "assert_step_not_stopped", _noop_stop_check)

    completion = await source_triage_set_flow.run_source_triage_set(
        run=SimpleNamespace(id="run-1"),
        db=object(),  # type: ignore[arg-type]
        runtime=object(),
        corpus=SimpleNamespace(run_view=object(), provider_index=None, vector_store_id=None),
        source_inventory=SourceMap(),
        workspace=deepcopy(stored),
        source_set=deepcopy(stored["source_sets"][0]),
    )

    assert completion.summary == "Recovered and mapped."
    assert seen_payloads[0]["set"]["execution_mode"] == "resume"
    assert seen_payloads[0]["set"]["source_assessment_count"] == 1


@pytest.mark.asyncio
async def test_set_flow_forwards_restricted_tool_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _workspace()
    seen_allowed_tool_names: list[set[str] | None] = []

    async def start(_db, **_kwargs):
        _set_status(stored, "in_progress")
        return deepcopy(stored)

    async def load(_db, _run_id):
        return deepcopy(stored)

    async def run_agent(_db, _run, _runtime, *, allowed_tool_names=None, **_kwargs):
        seen_allowed_tool_names.append(allowed_tool_names)
        _set_status(stored, "mapped")
        stored["source_sets"][0]["findings_summary"] = "Mapped."
        return SourceTriageCompletion(ready=True, summary="done", notes=[])

    monkeypatch.setattr(source_triage_set_flow, "EvidenceToolset", _EvidenceToolset)
    monkeypatch.setattr(source_triage_set_flow, "start_triage_source_set", start)
    monkeypatch.setattr(source_triage_set_flow, "load_source_triage_workspace", load)
    monkeypatch.setattr(source_triage_set_flow, "build_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(source_triage_set_flow, "build_source_triage_set_agent", run_agent)
    monkeypatch.setattr(source_triage_set_flow, "assert_step_not_stopped", _noop_stop_check)

    await source_triage_set_flow.run_source_triage_set(
        run=SimpleNamespace(id="run-1"),
        db=object(),  # type: ignore[arg-type]
        runtime=object(),
        corpus=SimpleNamespace(run_view=object(), provider_index=None, vector_store_id=None),
        source_inventory=SourceMap(),
        workspace=deepcopy(stored),
        source_set=deepcopy(stored["source_sets"][0]),
        allowed_tool_names={"read_triage_set_state", "finish_triage_source_set"},
    )

    assert seen_allowed_tool_names == [{"read_triage_set_state", "finish_triage_source_set"}]


@pytest.mark.asyncio
async def test_two_no_progress_returns_fail_without_needs_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _workspace()
    activations = 0
    unfinished_events: list[dict] = []

    async def start(_db, **_kwargs):
        _set_status(stored, "in_progress")
        return deepcopy(stored)

    async def load(_db, _run_id):
        return deepcopy(stored)

    async def run_agent(*_args, **_kwargs):
        nonlocal activations
        activations += 1
        return SourceTriageCompletion(ready=False, summary="Returned without finish.", notes=[])

    async def record_unfinished(**kwargs):
        unfinished_events.append(kwargs)

    monkeypatch.setattr(source_triage_set_flow, "EvidenceToolset", _EvidenceToolset)
    monkeypatch.setattr(source_triage_set_flow, "start_triage_source_set", start)
    monkeypatch.setattr(source_triage_set_flow, "load_source_triage_workspace", load)
    monkeypatch.setattr(source_triage_set_flow, "build_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(source_triage_set_flow, "build_source_triage_set_agent", run_agent)
    monkeypatch.setattr(source_triage_set_flow, "mark_source_triage_set_unfinished", record_unfinished)
    monkeypatch.setattr(source_triage_set_flow, "assert_step_not_stopped", _noop_stop_check)

    with pytest.raises(SourceTriageSetOperationalError, match="set-1 stalled"):
        await source_triage_set_flow.run_source_triage_set(
            run=SimpleNamespace(id="run-1"),
            db=object(),  # type: ignore[arg-type]
            runtime=object(),
            corpus=SimpleNamespace(run_view=object(), provider_index=None, vector_store_id=None),
            source_inventory=SourceMap(),
            workspace=deepcopy(stored),
            source_set=deepcopy(stored["source_sets"][0]),
        )

    assert activations == 2
    assert [event["stalled"] for event in unfinished_events] == [False, True]
    assert stored["source_sets"][0]["status"] == "in_progress"
    assert all(item.get("status") != "needs_repair" for item in stored["source_sets"])


@pytest.mark.asyncio
async def test_repeated_identical_resave_does_not_evade_no_progress_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _workspace()
    stored["source_assessments"] = [
        {
            "set_id": "set-1",
            "source_ref": source_ref,
            "processing_status": "resolved",
            "representation_status": "readable",
            "limitations": ["First caveat", "Second caveat"],
            "semantic_revision": 1,
            "updated_at": "initial",
        }
        for source_ref in ("S01", "S02")
    ]
    activations = 0
    unfinished_events: list[dict] = []

    async def start(_db, **_kwargs):
        _set_status(stored, "in_progress")
        return deepcopy(stored)

    async def load(_db, _run_id):
        return deepcopy(stored)

    async def run_agent(*_args, **_kwargs):
        nonlocal activations
        activations += 1
        # Emulate a handler replacing the same semantic records at the list tail.
        stored["source_assessments"].reverse()
        for assessment in stored["source_assessments"]:
            assessment["limitations"].reverse()
            assessment["semantic_revision"] += 1
            assessment["updated_at"] = f"activation-{activations}"
        return SourceTriageCompletion(ready=False, summary="No semantic change.", notes=[])

    async def record_unfinished(**kwargs):
        unfinished_events.append(kwargs)

    monkeypatch.setattr(source_triage_set_flow, "EvidenceToolset", _EvidenceToolset)
    monkeypatch.setattr(source_triage_set_flow, "start_triage_source_set", start)
    monkeypatch.setattr(source_triage_set_flow, "load_source_triage_workspace", load)
    monkeypatch.setattr(source_triage_set_flow, "build_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(source_triage_set_flow, "build_source_triage_set_agent", run_agent)
    monkeypatch.setattr(source_triage_set_flow, "mark_source_triage_set_unfinished", record_unfinished)
    monkeypatch.setattr(source_triage_set_flow, "assert_step_not_stopped", _noop_stop_check)

    with pytest.raises(SourceTriageSetOperationalError, match="set-1 stalled"):
        await source_triage_set_flow.run_source_triage_set(
            run=SimpleNamespace(id="run-1"),
            db=object(),  # type: ignore[arg-type]
            runtime=object(),
            corpus=SimpleNamespace(run_view=object(), provider_index=None, vector_store_id=None),
            source_inventory=SourceMap(),
            workspace=deepcopy(stored),
            source_set=deepcopy(stored["source_sets"][0]),
        )

    assert activations == 2
    assert [event["progress_saved"] for event in unfinished_events] == [False, False]
    assert [event["stalled"] for event in unfinished_events] == [False, True]


@pytest.mark.asyncio
async def test_unfinished_trace_does_not_turn_set_into_semantic_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import artifacts

    stored = _workspace()
    _set_status(stored, "in_progress")
    original = deepcopy(stored)
    traces: list[dict] = []

    class DB:
        def __init__(self) -> None:
            self.commits = 0

        async def commit(self) -> None:
            self.commits += 1

    async def save_trace(_db, **kwargs):
        traces.append(kwargs)

    db = DB()
    monkeypatch.setattr(artifacts, "save_tool_trace", save_trace)

    await source_triage_set_flow.mark_source_triage_set_unfinished(
        db=db,  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-1"),
        workspace=stored,
        set_id="set-1",
        reason="Reactivate the same set.",
        activation=1,
        progress_saved=False,
        stalled=False,
    )

    assert stored == original
    assert traces[0]["output_json"]["reactivate_same_set"] is True
    assert traces[0]["output_json"]["operational_failure"] is False
    assert db.commits == 1


@pytest.mark.asyncio
async def test_stage_does_not_advance_to_next_set_after_set_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _workspace()
    stored["source_order"] = ["S01", "S02"]
    stored["source_sets"].append(
        {
            "set_id": "set-2",
            "status": "planned",
            "source_refs": ["S02"],
            "question_ids": ["q-1"],
        }
    )
    executed: list[str] = []

    async def planner(*_args, **_kwargs):
        return SourceTriageCompletion(ready=True, summary="Planned.", notes=[])

    async def load(_db, _run_id):
        return deepcopy(stored)

    async def run_set(**kwargs):
        set_id = str(kwargs["source_set"]["set_id"])
        executed.append(set_id)
        return [SourceTriageCompletion(ready=False, summary=f"{set_id} did not finish.", notes=[])]

    monkeypatch.setattr(source_triage_stage, "build_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(source_triage_stage, "build_source_triage_planner_agent", planner)
    monkeypatch.setattr(source_triage_stage, "load_source_triage_workspace", load)
    monkeypatch.setattr(source_triage_stage, "run_source_mapper_for_set", run_set)
    monkeypatch.setattr(source_triage_stage, "assert_step_not_stopped", _noop_stop_check)

    runner = source_triage_stage.SourceTriageStageRunner(
        SimpleNamespace(id="run-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    context = source_triage_stage.SourceTriageStageContext(
        corpus=SimpleNamespace(),
        evidence_toolset=SimpleNamespace(),
        evidence_tools=[],
        source_inventory=SourceMap(),
        workspace=deepcopy(stored),
    )

    with pytest.raises(source_triage_stage.SourceMapperOperationalError):
        await runner.run_planner_and_sets(context)

    assert executed == ["set-1"]


@pytest.mark.asyncio
async def test_set_agent_disables_local_checkpoint_restarts() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.kwargs: dict | None = None

        async def call_structured(self, **kwargs):
            self.kwargs = kwargs
            return SourceTriageCompletion(ready=True, summary="Done.", notes=[])

    runtime = Runtime()
    await build_source_triage_set_agent(
        object(),  # type: ignore[arg-type]
        SimpleNamespace(
            id="run-1",
            config={"user_prompt": "Explain the evidence.", "duration_target": 1800},
        ),  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        source_set_payload={"set": {"set_id": "set-1"}},
        set_tools=[],
    )

    assert runtime.kwargs is not None
    assert runtime.kwargs["max_output_tokens"] is None
    assert runtime.kwargs["max_tool_rounds"] == 0
    assert runtime.kwargs["checkpoint_policy"].enabled is False
