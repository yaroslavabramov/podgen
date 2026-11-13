import json

import httpx2
import pytest

from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.openai_provider import OpenAIProvider


@pytest.mark.parametrize(
    "operation,path",
    [
        ("count", "/v1/responses/input_tokens"),
        ("submit", "/v1/responses"),
        ("retrieve", "/v1/responses/resp_test"),
    ],
)
def test_real_sdk_wire_archive_excludes_authorization(operation, path):
    captured = []
    secret = "sk-SYNTHETIC-test-secret-never-log"

    def handler(request):
        captured.append(request)
        return httpx2.Response(
            200,
            headers={"x-request-id": "req_test", "authorization": "DO_NOT_ARCHIVE_HEADERS"},
            json={"id": "resp_test", "input_tokens": 123},
        )

    provider = OpenAIProvider(secret, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    if operation == "count":
        reply = provider.count({"model": "gpt-5.4-2026-03-05", "input": "SYNTHETIC full input"})
    elif operation == "submit":
        reply = provider.submit(
            {"model": "gpt-5.4-2026-03-05", "input": "SYNTHETIC full input"}, client_request_id="synthetic-id"
        )
    else:
        reply = provider.retrieve("resp_test")
    assert captured[0].url.path == path
    assert captured[0].headers["authorization"] == "Bearer " + secret
    assert reply.request_id == "req_test"
    assert secret.encode() not in reply.body
    assert reply.request_body is None or secret.encode() not in reply.request_body
    assert b"DO_NOT_ARCHIVE_HEADERS" not in reply.body
    assert provider.client.max_retries == 0


def test_error_body_redaction_and_no_automatic_retry():
    secret = "sk-SYNTHETIC-secret-123456789"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(401, json={"error": {"message": "Incorrect API key provided: " + secret}})

    provider = OpenAIProvider(secret, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(ProviderFault) as caught:
        provider.submit({"model": "test", "input": "synthetic"}, client_request_id="id")
    assert len(requests) == 1
    assert secret.encode() not in caught.value.reply.body
    assert caught.value.reply.redacted is True
    assert caught.value.code == "provider_http_401" and caught.value.ambiguous is False


def test_sdk_preserves_explicit_cache_boundary_and_excludes_changing_tail():
    body = {
        "model": "gpt-6.1-terra",
        "instructions": "Review the episode.",
        "prompt_cache_options": {"mode": "explicit"},
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Evidence",
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    },
                    {"type": "input_text", "text": "Current script"},
                ],
            }
        ],
    }

    def server(request):
        sent = json.loads(request.content)
        assert sent["input"] == body["input"]
        if request.url.path.endswith("/input_tokens"):
            assert "prompt_cache_options" not in sent
        else:
            assert sent["prompt_cache_options"] == {"mode": "explicit"}
        return httpx2.Response(200, json={})

    with httpx2.Client(transport=httpx2.MockTransport(server)) as client:
        provider = OpenAIProvider("secret-test", http_client=client)
        provider.count(body)
        provider.submit(body, client_request_id="cache-test")


@pytest.mark.parametrize("status,ambiguous", [(429, False), (500, True), (503, True)])
def test_status_outcome_classification(status, ambiguous):
    provider = OpenAIProvider(
        "synthetic",
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(status, json={"error": {"message": "SYNTHETIC"}})
            )
        ),
    )
    with pytest.raises(ProviderFault) as caught:
        provider.submit({"model": "test", "input": "synthetic"}, client_request_id="id")
    assert caught.value.ambiguous is ambiguous


def test_transport_timeout_remains_ambiguous():
    def handler(request):
        raise httpx2.ReadTimeout("SYNTHETIC timeout", request=request)

    provider = OpenAIProvider("synthetic", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(ProviderFault) as caught:
        provider.submit({"model": "test", "input": "synthetic"}, client_request_id="id")
    assert caught.value.ambiguous


@pytest.mark.parametrize("name", ["plan-initial", "plan-continuation", "plan-count"])
def test_sdk3_preserves_actual_sdk2_longitude_payload_and_nested_order(name):
    from pathlib import Path

    path = Path(__file__).parent / "fixtures/research-cache-v1" / (name + ".wire.json")
    original = path.read_bytes()
    body = json.loads(original)
    requests = []

    def handler(request):
        requests.append(request.content)
        return httpx2.Response(200, json={"input_tokens": 100})

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        provider = OpenAIProvider("not-real", http_client=client)
        if name == "plan-count":
            provider.count(body)
        else:
            assert body["metadata"]["podgen_prompt_cache_layout"] == "stable-role-prefix/1"
            provider.submit(body, client_request_id="legacy-wire-check")
    actual = json.loads(requests[0])
    assert actual == body

    def ordered(value):
        if isinstance(value, dict):
            return [(key, ordered(item)) for key, item in value.items()]
        if isinstance(value, list):
            return [ordered(item) for item in value]
        return value

    # SDK 3 changes only the top-level JSON envelope's key order. Everything
    # inside it, including schemas, history and embedded JSON strings, is exact.
    for key in body:
        assert ordered(actual[key]) == ordered(body[key]), key
