"""Measure exactly the three model-visible wire components, before constructing any provider."""

import json

from podgen.domain.errors import ContractError
from podgen.writing.projections import CONTRACT, SINGLE_INPUT_TOKEN_LIMIT, VISIBLE_BYTES_LIMIT


def visible_sizes(body):
    values = dict(
        instructions=body["instructions"], input=body["input"], schema=body["text"]["format"]["schema"]
    )
    sizes = {
        name: len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
        for name, value in values.items()
    }
    return dict(**sizes, total=sum(sizes.values()))


def check_visible(body, contract, *, max_visible_bytes=VISIBLE_BYTES_LIMIT):
    if contract is None:
        return
    if contract != CONTRACT:
        raise ContractError("unknown_role_input_contract")
    if visible_sizes(body)["total"] > max_visible_bytes:
        raise ContractError("role_visible_byte_budget_exceeded")


def check_tokens(count, contract, *, max_input_tokens=SINGLE_INPUT_TOKEN_LIMIT):
    if contract == CONTRACT and count > max_input_tokens:
        raise ContractError("role_input_token_budget_exceeded")
