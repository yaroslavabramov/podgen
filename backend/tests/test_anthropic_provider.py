import json

import httpx
import pytest
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.messages_wire import messages_request

from tests.messages_cases import author_request


def test_author_request_translates_to_native_generation_and_count():
    request = author_request()
    expected = {
        "model": "claude-test",
        "system": "Write the episode.",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "A short brief."}]}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": {"type": "object", "properties": {}}},
        },
    }
    assert messages_request(request, counting=True) == expected
    assert messages_request(request) == dict(expected, max_tokens=256, stream=False)


def test_messages_transport_preserves_receipt_and_handles_ambiguous_failure():
    request = author_request()

    def server(req):
        if req.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 5})
        assert req.headers["x-client-request-id"] == "operation"
        return httpx.Response(
            200,
            headers={"request-id": "abc"},
            json={
                "type": "message",
                "role": "assistant",
                "model": request["model"],
                "id": "msg_foo",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 4,
                    "output_tokens": 7,
                },
                "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "script"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        provider = AnthropicProvider("private-value", http_client=client)
        assert json.loads(provider.count(request).body) == {"input_tokens": 5}
        reply = provider.submit(request, client_request_id="operation")
        assert json.loads(reply.body)["usage"] == {
            "input_tokens": 9,
            "output_tokens": 7,
            "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 4},
        }
        assert b"hidden" not in reply.body and b"hidden" in reply.wire_response
        assert json.loads(reply.request_body) == messages_request(request) and reply.request_id == "abc"

    def timeout(req):
        raise httpx.ReadTimeout("timeout", request=req)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        provider = AnthropicProvider("private-value", http_client=client)
        with pytest.raises(ProviderFault) as failure:
            provider.submit(request, client_request_id="operation")
        assert failure.value.ambiguous and failure.value.reply.request_body


def test_cache_boundary_and_changing_tail_reach_both_native_messages_endpoints():
    request = author_request()
    request["input"][0]["content"] = [
        {"type": "input_text", "text": "source passages", "prompt_cache_breakpoint": {"mode": "explicit"}},
        {"type": "input_text", "text": "current review"},
    ]
    expected = [
        {"type": "text", "text": "source passages", "cache_control": {"type": "ephemeral", "ttl": "5m"}},
        {"type": "text", "text": "current review"},
    ]
    for counting in (True, False):
        native = messages_request(request, counting=counting)
        assert native["messages"] == [{"role": "user", "content": expected}]
        assert "cache_control" not in native
    request["input"][0]["content"][0]["prompt_cache_breakpoint"]["ttl"] = "12h"
    with pytest.raises(ProviderFault, match="unsupported_anthropic_cache_breakpoint"):
        messages_request(request)
