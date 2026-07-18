from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.runtime_checkpoint_read_counts import read_counts_by_source_ref
from app.generation_v2.runtime_checkpoint_trace import trace_output_with_runtime
from app.generation_v2.runtime_tool_outcomes import (
    TOOL_OUTCOME_CONSTRAINT,
    TOOL_OUTCOME_CONTROL,
    TOOL_OUTCOME_MISS,
    TOOL_OUTCOME_SUCCESS,
    TOOL_OUTCOME_VALIDATION_ERROR,
    tool_output_outcome_kind,
)
from app.generation_v2.tools.evidence_toolset import EvidenceToolset


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self):
        return iter(self.values)


class _GuardDb:
    def __init__(self, reads: list[object]) -> None:
        self.results = [_ScalarResult([]), _ScalarResult(reads)]

    async def execute(self, _query):
        return self.results.pop(0)


def _read_trace(output: dict, *, source_ref: str = "S01") -> SimpleNamespace:
    return SimpleNamespace(
        tool_name="skim_source",
        input_json={"source_ref": source_ref},
        output_json=output,
        created_at=None,
    )


async def _patch_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _load(_db, _run_id):
        return {
            "corpus_revision": 2,
            "attempt": 1,
            "current_set": {"set_id": "set_1", "started_at": "2026-01-01T00:00:00"},
        }

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace_tracking.load_source_triage_workspace",
        _load,
    )


@pytest.mark.asyncio
async def test_more_than_ten_reads_remain_unblocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _patch_workspace(monkeypatch)
    toolset = EvidenceToolset(
        db=_GuardDb([_read_trace({"found": True}) for _ in range(25)]),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=SimpleNamespace(),  # type: ignore[arg-type]
        provider_index=None,
        triage_current_set_id="set_1",
    )

    async def _successful_read(_args):
        blocked = await toolset._triage_read_guard_error()
        return blocked or {"found": True, "items": [{"window_ref": "W01"}]}

    output = await toolset._call_with_research_commit_advisory(_successful_read, {})

    assert output["found"] is True
    assert "read_control" not in output


@pytest.mark.asyncio
async def test_triage_read_guard_never_blocks_on_a_read_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _patch_workspace(monkeypatch)
    toolset = EvidenceToolset(
        db=_GuardDb([_read_trace({"found": True}) for _ in range(10)]),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=SimpleNamespace(),  # type: ignore[arg-type]
        provider_index=None,
        triage_current_set_id="set_1",
    )

    async def _unblocked_read(_args):
        blocked = await toolset._triage_read_guard_error()
        assert blocked is None
        return {"found": True, "items": [{"window_ref": "W01"}]}

    output = await toolset._call_with_research_commit_advisory(_unblocked_read, {})

    assert output["found"] is True
    assert "read_control" not in output
    assert "outcome_kind" not in output


@pytest.mark.asyncio
async def test_empty_search_does_not_advance_read_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _patch_workspace(monkeypatch)
    toolset = EvidenceToolset(
        db=_GuardDb([_read_trace({"found": True}) for _ in range(9)]),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=SimpleNamespace(),  # type: ignore[arg-type]
        provider_index=None,
        triage_current_set_id="set_1",
    )

    async def _empty_search(_args):
        blocked = await toolset._triage_read_guard_error()
        return blocked or {"accepted": True, "returned_count": 0, "results": []}

    output = await toolset._call_with_research_commit_advisory(_empty_search, {})

    assert tool_output_outcome_kind(output) == TOOL_OUTCOME_MISS
    assert "read_control" not in output


def test_control_outcome_is_classified_separately_in_runtime_trace() -> None:
    output = {
        "outcome_kind": TOOL_OUTCOME_CONTROL,
        "control_code": "source_set_state_commit_required",
        "read_performed": False,
    }

    traced = trace_output_with_runtime(
        output,
        {
            "chars": 100,
            "model_facing_schema": "evidence_read",
            "contract_version": "v2.tool-output-contract.1",
        },
        chain_id="chain",
        checkpoint_index=0,
        chain_round=1,
    )

    assert tool_output_outcome_kind(output) == TOOL_OUTCOME_CONTROL
    assert traced["_runtime"]["tool_outcome_kind"] == TOOL_OUTCOME_CONTROL


def test_read_misses_constraints_and_invalid_calls_have_distinct_trace_outcomes() -> None:
    assert (
        tool_output_outcome_kind({"found": False, "error": "locator_not_found"})
        == TOOL_OUTCOME_MISS
    )
    assert (
        tool_output_outcome_kind({"found": False, "error": "evidence_too_large_use_source_window"})
        == TOOL_OUTCOME_CONSTRAINT
    )
    assert (
        tool_output_outcome_kind({"accepted": False, "error": "source_ref must belong to current set"})
        == TOOL_OUTCOME_VALIDATION_ERROR
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"accepted": True, "results": []},
        {"results": []},
        {"found": True, "items": []},
        {"found": True, "windows": []},
        {"accepted": True, "comparisons": []},
        {
            "accepted": True,
            "comparisons": [
                {"source_ref": "S01", "evidence": []},
                {"source_ref": "S02", "evidence": []},
            ],
        },
    ],
)
def test_explicit_empty_read_payload_is_a_miss(payload: dict) -> None:
    assert tool_output_outcome_kind(payload) == TOOL_OUTCOME_MISS


@pytest.mark.parametrize(
    "payload",
    [
        {"accepted": True, "results": [{"evidence_id": "E01"}]},
        {"found": True, "items": [{"window_ref": "W01"}]},
        {
            "accepted": True,
            "comparisons": [
                {"source_ref": "S01", "evidence": []},
                {"source_ref": "S02", "evidence": [{"evidence_id": "E01"}]},
            ],
        },
    ],
)
def test_nonempty_read_payload_is_success(payload: dict) -> None:
    assert tool_output_outcome_kind(payload) == TOOL_OUTCOME_SUCCESS


@pytest.mark.asyncio
async def test_read_progress_ignores_controls_and_failed_calls() -> None:
    class _Db:
        async def execute(self, _query):
            return _ScalarResult(
                [
                    _read_trace({"found": True}),
                    _read_trace({"accepted": True, "results": []}),
                    _read_trace(
                        {
                            "outcome_kind": TOOL_OUTCOME_CONTROL,
                            "read_performed": False,
                            "must_persist_before_more_reads": True,
                        }
                    ),
                    _read_trace({"accepted": False, "error": "invalid_window_ref"}),
                ]
            )

    counts = await read_counts_by_source_ref(
        _Db(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_set_agent_1",
    )

    assert counts == {"S01": 1}
