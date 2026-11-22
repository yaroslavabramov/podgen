"""Current Messages headers are explicit; archived profiles retain their original version."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.messages_versions import CURRENT_API, LEGACY_API, validate_wire
from podgen.runtime.models.messages_wire import messages_request
from podgen.runtime.models.provider_contracts import contract, validate

from tests.messages_cases import author_request


@pytest.mark.parametrize("version", [LEGACY_API, CURRENT_API])
def test_count_transport_retains_body_and_pinned_header(version):
    original = author_request()
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"input_tokens": 100})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("not-real", api_version=version, http_client=client)
        provider.count(original)
    assert sent[0].headers["anthropic-version"] == version
    assert json.loads(sent[0].content) == messages_request(original, counting=True)


def test_new_api_rejects_removed_fields_and_legacy_contract_has_no_new_gate():
    native = dict(
        messages=[
            dict(role="user", content=[dict(type="text", text="Exact source", citations=[{"source": "old"}])])
        ]
    )
    validate_wire(native, LEGACY_API)
    with pytest.raises(ContractError, match="user_citations"):
        validate_wire(native, CURRENT_API)
    native["messages"] = [dict(role="assistant", content=[dict(type="document")])]
    with pytest.raises(ContractError, match="assistant_document"):
        validate_wire(native, CURRENT_API)
    native["messages"] = []
    native["temperature"] = 1
    with pytest.raises(ContractError, match="sampling_parameters"):
        validate_wire(native, CURRENT_API)


def test_manifest_pin_is_independent_of_sdk_and_preserves_old_profile_selection():
    old = SimpleNamespace(
        provider="anthropic", api_family="messages", adapter_version="anthropic-messages-v1"
    )
    new = deepcopy(old)
    new.adapter_version = "anthropic-messages-v2"
    assert contract(old)["api_version"] == LEGACY_API
    assert contract(new)["api_version"] == CURRENT_API
    validate({}, "draft", old)
    pin = dict(provider_contracts={"draft": contract(old)}, runtime_versions={"openai_sdk": "2.54.0"})
    validate(pin, "draft", old)
    with pytest.raises(Conflict, match="provider_contract_binding"):
        validate(pin, "draft", new)
