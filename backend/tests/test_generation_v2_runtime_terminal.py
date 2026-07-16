from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.config import settings
from app.generation_v2.runtime import (
    AgentCheckpointPolicy,
    AgentTerminalResult,
    AgentTool,
    ToolPolicy,
    V2Runtime,
)
from app.generation_v2.runtime_contracts import terminal_result_from_tool_output
from app.models.generation_v2 import V2ToolTrace


class _Completion(BaseModel):
    ok: bool
    summary: str = ""


class _FakeResponses:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Runtime made an unexpected extra model call")
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = _FakeResponses(responses)


class _FakeDb:
    def __init__(self) -> None:
        self.info: dict = {}
        self.added: list[object] = []
        self.commits = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if isinstance(item, V2ToolTrace) and item.id is None:
                item.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1


def _tool_response(*calls: tuple[str, str, dict], input_tokens: int = 1_000) -> object:
    return SimpleNamespace(
        id=f"resp_{uuid.uuid4().hex}",
        status="completed",
        output_text="",
        output=[
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
            }
            for name, call_id, arguments in calls
        ],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=10,
            total_tokens=input_tokens + 10,
        ),
    )


def _structured_response(payload: str) -> object:
    return SimpleNamespace(
        id=f"resp_{uuid.uuid4().hex}",
        status="completed",
        output_text=payload,
        output=[],
        usage=SimpleNamespace(input_tokens=1_000, output_tokens=10, total_tokens=1_010),
    )


def _incomplete_response(
    *,
    reason: str | None = "max_output_tokens",
    output_tokens: int = 100,
    output: list[dict] | None = None,
) -> object:
    return SimpleNamespace(
        id=f"resp_{uuid.uuid4().hex}",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output_text="",
        output=output or [],
        usage=SimpleNamespace(
            input_tokens=1_000,
            output_tokens=output_tokens,
            total_tokens=1_000 + output_tokens,
        ),
    )


@pytest.mark.asyncio
async def test_terminal_in_mixed_batch_is_rejected_without_dropping_other_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    calls = {"finish": 0, "later": 0}

    async def finish(_args: dict) -> dict:
        calls["finish"] += 1
        return {
            "accepted": True,
            "status": "done",
            "source_id": str(uuid.uuid4()),
            "terminal_result": {"private": "x" * 20_000},
        }

    async def later(_args: dict) -> dict:
        calls["later"] += 1
        return {"accepted": True}

    fake_client = _FakeClient(
        [
            _tool_response(
                ("finish", "call_finish", {"summary": "finished durably"}),
                ("later", "call_later", {}),
            ),
            _tool_response(("finish", "call_finish_alone", {"summary": "finished durably"})),
        ]
    )
    fake_db = _FakeDb()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=fake_db,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_set_agent_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        local_tools=[
            AgentTool(
                name="finish",
                description="Finish",
                parameters={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
                handler=finish,
                terminal=True,
                terminal_result_builder=lambda arguments, _output: {
                    "ok": True,
                    "summary": arguments["summary"],
                },
            ),
            AgentTool(
                name="later",
                description="Must not run",
                parameters={"type": "object", "properties": {}},
                handler=later,
            ),
        ],
        tool_policy=ToolPolicy(
            file_search=False,
            local_tools=True,
            parallel_local_tool_calls=True,
        ),
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_tool_rounds=3,
    )

    assert result == _Completion(ok=True, summary="finished durably")
    assert calls == {"finish": 1, "later": 1}
    assert len(fake_client.responses.calls) == 2
    assert fake_client.responses.calls[0]["parallel_tool_calls"] is True
    finish_traces = [item for item in fake_db.added if isinstance(item, V2ToolTrace) and item.tool_name == "finish"]
    assert finish_traces[0].output_json["error"] == "terminal_tool_must_be_called_alone"
    assert finish_traces[1].output_json["accepted"] is True
    assert any(isinstance(item, V2ToolTrace) and item.tool_name == "later" for item in fake_db.added)


@pytest.mark.asyncio
async def test_single_accepted_terminal_finishes_before_checkpoint_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    async def finish(_args: dict) -> dict:
        return {"accepted": True}

    fake_client = _FakeClient(
        [_tool_response(("finish", "call_finish", {"summary": "done"}), input_tokens=70_000)]
    )
    fake_db = _FakeDb()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=fake_db,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_set_agent_terminal_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        local_tools=[
            AgentTool(
                name="finish",
                description="Finish",
                parameters={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
                handler=finish,
                terminal=True,
                terminal_result_builder=lambda arguments, _output: {
                    "ok": True,
                    "summary": arguments["summary"],
                },
            )
        ],
        tool_policy=ToolPolicy(file_search=False, local_tools=True, parallel_local_tool_calls=True),
        checkpoint_policy=AgentCheckpointPolicy(
            enabled=True,
            max_chain_input_tokens=1,
            max_chain_rounds=1,
            max_chain_model_tool_output_chars=1,
        ),
        max_tool_rounds=2,
    )

    assert result == _Completion(ok=True, summary="done")
    assert len(fake_client.responses.calls) == 1
    assert not any(
        isinstance(item, V2ToolTrace) and item.tool_name == "runtime.checkpoint_restart"
        for item in fake_db.added
    )


@pytest.mark.asyncio
async def test_opted_in_local_tools_persist_multiple_independent_calls_in_one_provider_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    written: list[str] = []

    async def write(args: dict) -> dict:
        written.append(str(args["value"]))
        return {"accepted": True, "value": args["value"]}

    async def finish(_args: dict) -> dict:
        return {"accepted": True}

    fake_client = _FakeClient(
        [
            _tool_response(
                ("write", "call_write_1", {"value": "one"}),
                ("write", "call_write_2", {"value": "two"}),
            ),
            _tool_response(("finish", "call_finish", {})),
        ]
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=_FakeDb(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_set_agent_batch_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        local_tools=[
            AgentTool(
                name="write",
                description="Write one independent item",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                handler=write,
            ),
            AgentTool(
                name="finish",
                description="Finish",
                parameters={"type": "object", "properties": {}},
                handler=finish,
                terminal=True,
                terminal_result_builder=lambda _arguments, _output: {"ok": True},
            ),
        ],
        tool_policy=ToolPolicy(
            file_search=False,
            local_tools=True,
            parallel_local_tool_calls=True,
        ),
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_tool_rounds=3,
    )

    assert result == _Completion(ok=True)
    assert written == ["one", "two"]
    assert len(fake_client.responses.calls) == 2
    assert all(call["parallel_tool_calls"] is True for call in fake_client.responses.calls)


@pytest.mark.asyncio
async def test_rejected_terminal_continues_until_terminal_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    builder_calls = 0

    handler_calls = 0

    async def finish(_args: dict) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        if handler_calls == 1:
            return {"accepted": False, "error": "missing durable state"}
        return {"accepted": True}

    def build_completion(_arguments: dict, _output: dict) -> dict:
        nonlocal builder_calls
        builder_calls += 1
        return {"ok": True}

    fake_client = _FakeClient(
        [
            _tool_response(("finish", "call_finish", {})),
            _tool_response(("finish", "call_finish_repaired", {})),
        ]
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=_FakeDb(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_set_agent_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        local_tools=[
            AgentTool(
                name="finish",
                description="Finish",
                parameters={"type": "object", "properties": {}},
                handler=finish,
                terminal=True,
                terminal_result_builder=build_completion,
            )
        ],
        tool_policy=ToolPolicy(file_search=False, local_tools=True),
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_tool_rounds=3,
    )

    assert result == _Completion(ok=True)
    assert handler_calls == 2
    assert builder_calls == 1
    assert len(fake_client.responses.calls) == 2
    assert all(call["parallel_tool_calls"] is False for call in fake_client.responses.calls)
    assert fake_client.responses.calls[1]["previous_response_id"] is not runtime_module.openai.NOT_GIVEN
    assert fake_client.responses.calls[1]["input"][0]["type"] == "function_call_output"


@pytest.mark.asyncio
async def test_terminal_workflow_rejects_plain_structured_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    async def finish(_args: dict) -> dict:
        return {"accepted": False, "error": "repair required"}

    fake_client = _FakeClient(
        [
            _tool_response(("finish", "call_finish", {})),
            _structured_response('{"ok":true,"summary":"bypassed"}'),
        ]
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match="without completing its required terminal tool"):
        await V2Runtime().call_structured(
            db=_FakeDb(),  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_source_triage",
            agent_name="source_triage_set_agent_test",
            schema_model=_Completion,
            system="system",
            prompt="prompt",
            local_tools=[
                AgentTool(
                    name="finish",
                    description="Finish",
                    parameters={"type": "object", "properties": {}},
                    handler=finish,
                    terminal=True,
                    terminal_result_builder=lambda _arguments, _output: {"ok": True},
                )
            ],
            tool_policy=ToolPolicy(file_search=False, local_tools=True),
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            max_tool_rounds=3,
        )


@pytest.mark.asyncio
async def test_max_output_incomplete_continues_same_chain_without_spending_workflow_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    first = _incomplete_response(reason=None, output_tokens=100)
    fake_client = _FakeClient([first, _structured_response('{"ok":true,"summary":"continued"}')])
    fake_db = _FakeDb()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=fake_db,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_planner_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        tool_policy=ToolPolicy(file_search=False, local_tools=False),
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_output_tokens=100,
        max_tool_rounds=1,
    )

    assert result == _Completion(ok=True, summary="continued")
    assert len(fake_client.responses.calls) == 2
    assert fake_client.responses.calls[1]["previous_response_id"] == first.id
    assert "exhausted its output budget" in fake_client.responses.calls[1]["input"][0]["content"]
    response_traces = [
        item
        for item in fake_db.added
        if isinstance(item, V2ToolTrace) and item.tool_name == "openai.responses.create"
    ]
    assert response_traces[0].output_json["incomplete_reason"] is None
    assert response_traces[0].output_json["max_output_tokens"] == 100


@pytest.mark.asyncio
async def test_provider_native_output_window_is_omitted_and_incomplete_response_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    first = _incomplete_response(reason="max_output_tokens", output_tokens=32_000)
    fake_client = _FakeClient([first, _structured_response('{"ok":true,"summary":"continued"}')])
    fake_db = _FakeDb()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=fake_db,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_planner_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        tool_policy=ToolPolicy(file_search=False, local_tools=False),
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_output_tokens=None,
        max_tool_rounds=1,
    )

    assert result == _Completion(ok=True, summary="continued")
    assert fake_client.responses.calls[0]["max_output_tokens"] is runtime_module.openai.NOT_GIVEN
    assert fake_client.responses.calls[1]["previous_response_id"] == first.id
    trace = next(
        item
        for item in fake_db.added
        if isinstance(item, V2ToolTrace) and item.tool_name == "openai.responses.create"
    )
    assert trace.output_json["max_output_tokens"] is None


@pytest.mark.asyncio
async def test_provider_native_output_exhaustion_error_never_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    fake_client = _FakeClient([_incomplete_response(), _incomplete_response(), _incomplete_response()])
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_max_tool_rounds", 2)
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match=r"max_output_tokens=provider_native") as exc_info:
        await V2Runtime().call_structured(
            db=_FakeDb(),  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_source_triage",
            agent_name="source_triage_planner_test",
            schema_model=_Completion,
            system="system",
            prompt="prompt",
            tool_policy=ToolPolicy(file_search=False, local_tools=False),
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            max_output_tokens=None,
            max_tool_rounds=1,
        )

    assert "max_output_tokens=None" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_output_continuation_preserves_persisted_hosted_evidence_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    first = _incomplete_response(
        output=[
            {
                "type": "file_search_call",
                "id": "fs_1",
                "status": "completed",
                "queries": ["mechanism"],
                "results": [{"file_id": "file_1", "filename": "report.pdf", "text": "Evidence preview"}],
            }
        ]
    )
    fake_client = _FakeClient([first, _structured_response('{"ok":true,"summary":"continued"}')])
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    async def select_evidence(_args: dict) -> dict:
        raise AssertionError("No local tool should be invoked")

    async def persist_hosted(_calls, _response_id, _phase, _agent_name):
        return {
            "observed_evidence": [
                {
                    "id": "ev-1",
                    "source_ref": "S01",
                    "source_title": "Report",
                    "source_domain": "example.com",
                    "locator_text": "page 2",
                    "snippet_text": "Evidence preview",
                }
            ]
        }

    result = await V2Runtime().call_structured(
        db=_FakeDb(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_planner_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        vector_store_id="vs_test",
        local_tools=[
            AgentTool(
                name="select_evidence",
                description="Select",
                parameters={"type": "object", "properties": {}},
                handler=select_evidence,
            )
        ],
        tool_policy=ToolPolicy(
            file_search=True,
            local_tools=True,
            file_search_source_version_ids=(str(uuid.uuid4()),),
        ),
        hosted_file_search_handler=persist_hosted,
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_output_tokens=100,
        max_tool_rounds=1,
    )

    assert result == _Completion(ok=True, summary="continued")
    continuation_input = fake_client.responses.calls[1]["input"]
    assert "ev-1" in continuation_input[0]["content"]
    assert "exhausted its output budget" in continuation_input[1]["content"]


@pytest.mark.asyncio
async def test_repeated_max_output_incomplete_fails_with_explicit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    fake_client = _FakeClient([_incomplete_response(), _incomplete_response(), _incomplete_response()])
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_max_tool_rounds", 2)
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match=r"exhausted max_output_tokens=100 for 3 consecutive"):
        await V2Runtime().call_structured(
            db=_FakeDb(),  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_source_triage",
            agent_name="source_triage_planner_test",
            schema_model=_Completion,
            system="system",
            prompt="prompt",
            tool_policy=ToolPolicy(file_search=False, local_tools=False),
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            max_output_tokens=100,
            max_tool_rounds=1,
        )

    assert len(fake_client.responses.calls) == 3


@pytest.mark.asyncio
async def test_non_output_incomplete_reason_fails_without_parsing_empty_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    fake_client = _FakeClient([_incomplete_response(reason="content_filter")])
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match="incomplete provider response.*content_filter"):
        await V2Runtime().call_structured(
            db=_FakeDb(),  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_source_triage",
            agent_name="source_triage_planner_test",
            schema_model=_Completion,
            system="system",
            prompt="prompt",
            tool_policy=ToolPolicy(file_search=False, local_tools=False),
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            max_output_tokens=100,
        )


@pytest.mark.asyncio
async def test_failed_provider_response_reports_error_and_output_status_without_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    handler_calls = 0

    async def write(_args: dict) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return {"accepted": True}

    response = SimpleNamespace(
        id="resp_failed",
        status="failed",
        error=SimpleNamespace(code="provider_failure", message="request could not be completed"),
        output_text="",
        output=[
            {
                "type": "function_call",
                "name": "write",
                "call_id": "call_failed",
                "arguments": "{}",
                "status": "incomplete",
            }
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=0, total_tokens=10),
    )
    fake_db = _FakeDb()
    fake_client = _FakeClient([response])
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match="request could not be completed"):
        await V2Runtime().call_structured(
            db=fake_db,  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_source_triage",
            agent_name="source_triage_planner_test",
            schema_model=_Completion,
            system="system",
            prompt="prompt",
            local_tools=[
                AgentTool(
                    name="write",
                    description="Write",
                    parameters={"type": "object", "properties": {}},
                    handler=write,
                )
            ],
            tool_policy=ToolPolicy(file_search=False, local_tools=True),
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        )

    assert handler_calls == 0
    trace = next(
        item
        for item in fake_db.added
        if isinstance(item, V2ToolTrace) and item.tool_name == "openai.responses.create"
    )
    assert trace.output_json["provider_error"]["code"] == "provider_failure"
    assert trace.output_json["output_item_statuses"][0]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_incomplete_or_malformed_function_call_never_invokes_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    handler_calls = 0

    async def write(_args: dict) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return {"accepted": True}

    broken_call = {
        "type": "function_call",
        "name": "write",
        "call_id": "call_broken",
        "arguments": '{"value":',
        "status": "incomplete",
    }
    fake_client = _FakeClient(
        [
            _incomplete_response(output=[broken_call]),
            _structured_response('{"ok":true,"summary":"recovered"}'),
        ]
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    result = await V2Runtime().call_structured(
        db=_FakeDb(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_planner_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        local_tools=[
            AgentTool(
                name="write",
                description="Write",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}},
                handler=write,
            )
        ],
        tool_policy=ToolPolicy(file_search=False, local_tools=True),
        checkpoint_policy=AgentCheckpointPolicy(enabled=False),
        max_output_tokens=100,
        max_tool_rounds=2,
    )

    assert result == _Completion(ok=True, summary="recovered")
    assert handler_calls == 0
    tool_output = json.loads(fake_client.responses.calls[1]["input"][0]["output"])
    assert tool_output["error"] == "incomplete_function_call"


@pytest.mark.asyncio
async def test_completed_terminal_resume_returns_without_openai_or_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime as runtime_module

    async def handler(_args: dict) -> dict:
        raise AssertionError("Completed terminal handler must not run")

    async def resume() -> AgentTerminalResult:
        return AgentTerminalResult(
            accepted=True,
            completion={"ok": True, "summary": "already complete"},
        )

    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(
        runtime_module,
        "get_openai_client",
        lambda: (_ for _ in ()).throw(AssertionError("OpenAI client must not be created")),
    )

    result = await V2Runtime().call_structured(
        db=_FakeDb(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_set_agent_test",
        schema_model=_Completion,
        system="system",
        prompt="prompt",
        local_tools=[
            AgentTool(
                name="finish",
                description="Finish",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                terminal=True,
                terminal_resume_loader=resume,
            )
        ],
        tool_policy=ToolPolicy(file_search=False, local_tools=True),
    )

    assert result == _Completion(ok=True, summary="already complete")


def test_terminal_output_contract_supports_reserved_completion_payload() -> None:
    async def handler(_args: dict) -> dict:
        return {}

    tool = AgentTool(
        name="finish",
        description="Finish",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        terminal=True,
    )

    result = terminal_result_from_tool_output(
        tool,
        arguments={},
        output={
            "accepted": True,
            "terminal_result": {"ok": True, "summary": "from output"},
        },
    )

    assert result == AgentTerminalResult(
        accepted=True,
        completion={"ok": True, "summary": "from output"},
    )
