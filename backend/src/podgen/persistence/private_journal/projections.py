"""Lossless archive views over immutable reusable request context."""

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError


def render(adapter, descriptor, payload):
    version = descriptor.get("projection", "identity/3")
    if version == "identity/3":
        return payload
    if version != "physical-request/3" or payload.get("contract") != "model-request-v1":
        raise ContractError("unsupported_private_archive_view")
    result = dict(payload)
    reference = RuntimeRef.model_validate(result.pop("context_parent_ref"))
    overrides = result.pop("context_overrides")
    parent = adapter.read(reference)
    result["context"] = dict(parent["context"], **overrides)
    return result
