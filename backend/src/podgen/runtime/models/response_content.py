"""Shared parsing for live and archived provider responses."""

from podgen.domain.errors import ContractError


def binding_error(response, request, expected_id):
    if response.get("model") != request["model"]:
        return "provider_model_revision_mismatch"
    if expected_id is not None and response.get("id") != expected_id:
        return "provider_response_id_mismatch"
    metadata = response.get("metadata")
    if not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in request["metadata"].items()):
        return "provider_response_metadata_mismatch"
    return None


def output_text(response, *, web_search=False):
    output = response.get("output")
    if not isinstance(output, list):
        raise ContractError("provider_output_missing")
    parts = []
    for item in output:
        if not isinstance(item, dict):
            raise ContractError("provider_malformed_output")
        if item.get("type") == "web_search_call" and web_search:
            continue
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ContractError("unexpected_provider_tool_or_output")
        if item.get("status") not in (None, "completed"):
            raise ContractError("provider_message_incomplete")
        contents = item.get("content")
        if not isinstance(contents, list):
            raise ContractError("provider_malformed_content")
        for content in contents:
            if not isinstance(content, dict):
                raise ContractError("provider_malformed_content")
            if content.get("type") == "refusal":
                raise ContractError("provider_refused")
            if content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                raise ContractError("provider_output_missing")
            parts.append(content["text"])
    if not parts:
        raise ContractError("provider_output_missing")
    return "".join(parts)
