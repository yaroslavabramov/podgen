"""Read semantic user data from both archived single-part and pinned multipart requests."""

from podgen.domain.errors import ContractError
from podgen.writing.prompt_cache import VERSION

from .validation import load_unique_json


def request_payload(body):
    version = body.get("metadata", {}).get("podgen_prompt_cache_layout")
    if version not in (None, "", VERSION):
        raise ContractError("unsupported_prompt_cache_layout")
    if version != VERSION:
        return load_unique_json(body["input"][0]["content"][0]["text"])
    users = [message for message in body["input"] if message["role"] == "user"]
    if len(users) != 1:
        raise ContractError("invalid_multipart_user_payload")
    result = {}
    for part in users[0]["content"]:
        if part["type"] != "input_text":
            raise ContractError("invalid_multipart_user_payload")
        value = load_unique_json(part["text"])
        if not isinstance(value, dict):
            raise ContractError("invalid_multipart_user_payload")
        for key, item in value.items():
            if key in result:
                raise ContractError("duplicate_multipart_payload_field", key)
            result[key] = item
    if "additional_support" in result:
        extra = result.pop("additional_support")
        support = result.setdefault("support", [])
        if not isinstance(support, list) or not isinstance(extra, list):
            raise ContractError("invalid_multipart_user_payload")
        support.extend(extra)
    return result
