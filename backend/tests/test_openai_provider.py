import httpx
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
        return httpx.Response(
            200,
            headers={"x-request-id": "req_test", "authorization": "DO_NOT_ARCHIVE_HEADERS"},
            json={"id": "resp_test", "input_tokens": 123},
        )

    provider = OpenAIProvider(secret, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
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
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided: " + secret}})

    provider = OpenAIProvider(secret, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderFault) as caught:
        provider.submit({"model": "test", "input": "synthetic"}, client_request_id="id")
    assert len(requests) == 1
    assert secret.encode() not in caught.value.reply.body
    assert caught.value.reply.redacted is True
    assert caught.value.code == "provider_http_401" and caught.value.ambiguous is False


@pytest.mark.parametrize("status,ambiguous", [(429, False), (500, True), (503, True)])
def test_status_outcome_classification(status, ambiguous):
    provider = OpenAIProvider(
        "synthetic",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={"error": {"message": "SYNTHETIC"}})
            )
        ),
    )
    with pytest.raises(ProviderFault) as caught:
        provider.submit({"model": "test", "input": "synthetic"}, client_request_id="id")
    assert caught.value.ambiguous is ambiguous


def test_transport_timeout_remains_ambiguous():
    def handler(request):
        raise httpx.ReadTimeout("SYNTHETIC timeout", request=request)

    provider = OpenAIProvider("synthetic", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderFault) as caught:
        provider.submit({"model": "test", "input": "synthetic"}, client_request_id="id")
    assert caught.value.ambiguous
