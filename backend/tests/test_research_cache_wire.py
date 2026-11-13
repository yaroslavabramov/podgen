"""Actual SDK/native transports over local HTTP mocks; no provider service is contacted."""

import json
from copy import deepcopy

import httpx
import httpx2
import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.research_cache_layout import VERSION, content, mark_turn, validate_boundaries
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.messages_versions import CURRENT_API, LEGACY_API, api_version
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.responses_wire import responses_request
from tests.test_source_tool_runtime import call, final, setup


def markers(value):
    if isinstance(value, dict):
        return sum(k in ("cache_control", "prompt_cache_breakpoint") for k in value) + sum(
            markers(v) for v in value.values()
        )
    return sum(markers(v) for v in value) if isinstance(value, list) else 0


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_real_tool_loop_count_submit_and_resume_keep_exact_results(monkeypatch, provider):
    journal, profile, limits, corpus = setup(monkeypatch, provider, turns=5)
    body = journal.request(journal.root)
    semantic = dict(
        assignment={"question": "Exact question"},
        research_contents=[{"material_id": "M1"}],
        source_catalog=[{"source_id": "S1"}],
        script={"text": "Exact changing task"},
    )
    body["metadata"]["podgen_prompt_cache_layout"] = VERSION
    body["input"] = [dict(role="user", content=content(semantic, "draft"))]
    journal.root["request_record_id"] = journal.store_request(body)
    assert request_payload(body) == semantic
    submits, counts = [], []
    transport = httpx2 if provider == "openai" else httpx

    def handler(request):
        native = json.loads(request.content)
        counting = request.url.path.endswith(("/input_tokens", "/count_tokens"))
        (counts if counting else submits).append(native)
        validate_boundaries(native)
        assert markers(native) == 2 + min(len(submits) if counting else len(submits) - 1, 2)
        if counting:
            assert "metadata" not in native
            return transport.Response(200, json={"input_tokens": 100})
        turn = len(submits)
        if provider == "openai":
            results = [item for item in native["input"] if item.get("type") == "function_call_output"]
            for item in results:
                assert isinstance(item["output"], list) and len(item["output"]) == 1
                assert item["output"][0]["type"] == "input_text"
                text = item["output"][0]["text"]
                assert json.loads(text) == {
                    "passage": {
                        "read_id": f"E{int(item['call_id'].split('_')[1])}",
                        "text": "Full exact source passage",
                    }
                }
            response = dict(
                id=f"resp_{turn}",
                model=native["model"],
                metadata=native["metadata"],
                status="completed",
                output=[call(turn)] if turn < 5 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            )
        else:
            assert request.headers["anthropic-version"] == CURRENT_API
            results = [
                piece
                for message in native["messages"]
                for piece in message["content"]
                if piece["type"] == "tool_result"
            ]
            assert all(
                isinstance(item["content"], str)
                and "prompt_cache" not in item["content"]
                and "cache_control" not in item["content"]
                for item in results
            )
            blocks = (
                [
                    dict(type="thinking", thinking=f"Reasoning {turn}", signature=f"signature_{turn}"),
                    dict(
                        type="tool_use",
                        id=f"call_{turn}",
                        name="read_source",
                        input=dict(source_id="S1", start=0, end=4000),
                    ),
                ]
                if turn < 5
                else [dict(type="text", text='{"answer":"done"}')]
            )
            response = dict(
                type="message",
                role="assistant",
                id=f"msg_{turn}",
                model=native["model"],
                content=blocks,
                stop_reason="tool_use" if turn < 5 else "end_turn",
                usage=dict(input_tokens=100, output_tokens=10),
            )
        return transport.Response(200, json=response)

    with transport.Client(transport=transport.MockTransport(handler)) as client:
        factory = (
            (lambda _: OpenAIProvider("not-real", http_client=client))
            if provider == "openai"
            else (lambda _: AnthropicProvider("not-real", http_client=client))
        )
        gateway = ModelGateway(journal, factory, source_corpus=corpus)

        class PausedAfterRead(Exception):
            pass

        def pause_after_second_receipt():
            if len(corpus.calls) == 2:
                raise PausedAfterRead()

        with pytest.raises(PausedAfterRead):
            gateway.invoke(
                journal.root.copy(),
                profile,
                limits=limits,
                totals=dict(input=0, output=0),
                guard=pause_after_second_receipt,
            )
        assert len(submits) == 2 and len(corpus.calls) == 2
        for _ in range(2):
            assert (
                gateway.invoke(
                    journal.root.copy(),
                    profile,
                    limits=limits,
                    totals=dict(input=0, output=0),
                    guard=lambda: None,
                )
                == '{"answer":"done"}'
            )
    assert len(submits) == len(counts) == 5 and len(corpus.calls) == 4
    assert [markers(body) for body in submits] == [2, 3, 4, 4, 4]
    assert journal.request(journal.root) == body

    def without_markers(value):
        if isinstance(value, dict):
            return {
                key: without_markers(item)
                for key, item in value.items()
                if key not in {"cache_control", "prompt_cache_breakpoint"}
            }
        if isinstance(value, list):
            return [without_markers(item) for item in value]
        return value

    history_key = "input" if provider == "openai" else "messages"
    for previous, current in zip(submits, submits[1:], strict=False):
        old_history = without_markers(previous[history_key])
        new_history = without_markers(current[history_key])
        assert new_history[: len(old_history)] == old_history
        # Native system/tool prefixes are unchanged as well as every earlier message.
        for key in previous.keys() & {"instructions", "system", "tools"}:
            assert without_markers(current[key]) == without_markers(previous[key])
    for counted, submitted in zip(counts, submits, strict=True):
        assert counted[history_key] == submitted[history_key]


def test_v1_routing_is_identity_and_global_native_boundary_limit_includes_system_tools():
    legacy = dict(metadata={"podgen_prompt_cache_layout": "stable-role-prefix/1"}, input=[])
    assert responses_request(legacy) is legacy
    unchanged = deepcopy(legacy)
    mark_turn(legacy)
    assert legacy == unchanged
    with pytest.raises(ContractError, match="breakpoint_limit"):
        validate_boundaries(
            dict(
                system=[dict(cache_control=dict(type="ephemeral"))],
                tools=[dict(cache_control=dict(type="ephemeral"))],
                messages=[dict(content=[dict(cache_control=dict(type="ephemeral")) for _ in range(3)])],
            )
        )
    assert api_version("anthropic-messages-v1") == LEGACY_API
    assert api_version("anthropic-messages-v2") == CURRENT_API


def test_role_specific_prefix_keeps_selected_sections_and_never_caches_cold_script():
    first = dict(
        assignment={"question": "q"},
        research_contents=[{"material_id": "M1"}],
        source_catalog=[],
        editorial_frame={"route_summary": "first composition"},
        support=[{"card_id": "M1", "text": "Full selected section"}],
        script={"utterances": ["first"]},
    )
    second = deepcopy(first)
    second["editorial_frame"] = {"route_summary": "revised composition"}
    second["script"] = {"utterances": ["second"]}
    second["support"].append({"card_id": "M2", "text": "Full repair-specific section"})
    a = content(first, "revise-1", stable_support_ids=["M1"])
    b = content(second, "revise-2", stable_support_ids=["M1"])
    assert a[0] == b[0]
    assert json.loads(a[0]["text"])["support"] == first["support"]
    assert json.loads(b[-1]["text"])["additional_support"] == second["support"][1:]
    cold = content(
        dict(
            audience="Exact audience", script={"utterances": ["changing"]}, audit_context={"phase": "first"}
        ),
        "reader-review",
    )
    assert "prompt_cache_breakpoint" in cold[0] and "prompt_cache_breakpoint" not in cold[-1]
    for payload, parts in ((first, a), (second, b)):
        assert (
            request_payload(
                dict(
                    metadata={"podgen_prompt_cache_layout": VERSION}, input=[dict(role="user", content=parts)]
                )
            )
            == payload
        )
