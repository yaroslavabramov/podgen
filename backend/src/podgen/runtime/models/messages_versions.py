"""Messages API selection belongs to the immutable model profile, not installed defaults."""

from podgen.domain.errors import ContractError

LEGACY_ADAPTER = "anthropic-messages-v1"
CURRENT_ADAPTER = "anthropic-messages-v2"
LEGACY_API = "2023-06-01"
CURRENT_API = "2026-10-21"
VERSIONS = {LEGACY_ADAPTER: LEGACY_API, CURRENT_ADAPTER: CURRENT_API}


def api_version(adapter):
    if adapter not in VERSIONS:
        raise ContractError("unsupported_messages_adapter")
    return VERSIONS[adapter]


def validate_wire(body, version):
    if version == LEGACY_API:
        return
    if version != CURRENT_API:
        raise ContractError("unsupported_messages_api_version")
    if {"temperature", "top_p", "top_k"} & body.keys():
        raise ContractError("messages_sampling_parameters_removed")
    for message in body["messages"]:
        for piece in message["content"]:
            if message["role"] == "assistant" and piece.get("type") == "document":
                raise ContractError("messages_assistant_document_removed")
            if message["role"] == "user":
                blocks = [piece]
                if piece.get("type") == "search_result":
                    blocks.extend(piece.get("content", []))
                if any(block.get("type") == "text" and block.get("citations") for block in blocks):
                    raise ContractError("messages_user_citations_removed")
