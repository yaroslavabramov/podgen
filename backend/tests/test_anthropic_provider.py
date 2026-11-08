import json
from pathlib import Path

import httpx
import pytest

from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.messages_wire import messages_request

ARCHIVE = Path(__file__).resolve().parents[2] / "evals/reports/2026-11-08-snow-narrative-rebuild/calls"


@pytest.mark.parametrize(
    "directory", sorted(ARCHIVE.glob("*/messages-request.json")), ids=lambda p: p.parent.name
)
def test_messages_matches_actual_historical_author_request(directory):
    original = json.loads((directory.parent / "podgen-request.json").read_bytes())
    expected = json.loads(directory.read_bytes())
    assert messages_request(original) == expected
    assert messages_request(original, counting=True) == {
        key: value for key, value in expected.items() if key not in ("stream", "max_tokens")
    }


def test_messages_transport_preserves_receipt_and_handles_ambiguous_failure():
    request = json.loads((ARCHIVE / "05-draft/podgen-request.json").read_bytes())

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
                "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "output_tokens": 7},
                "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "script"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        provider = AnthropicProvider("private-value", http_client=client)
        assert json.loads(provider.count(request).body) == {"input_tokens": 5}
        reply = provider.submit(request, client_request_id="operation")
        assert json.loads(reply.body)["usage"] == {"input_tokens": 5, "output_tokens": 7}
        assert b"hidden" not in reply.body and b"hidden" in reply.wire_response
        assert json.loads(reply.request_body) == messages_request(request) and reply.request_id == "abc"

    def timeout(req):
        raise httpx.ReadTimeout("timeout", request=req)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        provider = AnthropicProvider("private-value", http_client=client)
        with pytest.raises(ProviderFault) as failure:
            provider.submit(request, client_request_id="operation")
        assert failure.value.ambiguous and failure.value.reply.request_body
