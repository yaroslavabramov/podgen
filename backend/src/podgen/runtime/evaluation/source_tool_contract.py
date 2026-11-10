"""Versioned orchestration and frozen tool declarations; no inference for historical manifests."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .reading_workflow import base_stage

KEY = "source_tool_contract"
LEGACY_VERSION = "frozen-corpus-tools/1"
FINAL_TURN_VERSION = "frozen-corpus-tools/2"
READ_BUDGET_VERSION = "frozen-corpus-tools/3"
VERSION = "frozen-corpus-tools/4"
ROLES = ("draft", "revise", "fact-review")


def create(behavior):
    if behavior.name != "production/1":
        return None
    from podgen.retrieval.config import profile
    from podgen.writing.source_tools import definitions

    return dict(
        version=VERSION,
        definitions=definitions(retrieval=True),
        retrieval=profile(),
        max_model_turns=12,
        max_tool_calls=24,
        final_turn="structured_without_tools",
        max_read_chars=9600,
    )


def from_manifest(manifest):
    value = manifest.get(KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractError("unsupported_source_tool_contract")
    fields = {"version", "definitions", "max_model_turns", "max_tool_calls"}
    if value.get("version") in (FINAL_TURN_VERSION, READ_BUDGET_VERSION, VERSION):
        fields.add("final_turn")
        if value.get("final_turn") != "structured_without_tools":
            raise ContractError("invalid_source_tool_contract")
    if value.get("version") == VERSION:
        from podgen.retrieval.config import validate

        fields.add("retrieval")
        validate(value.get("retrieval"))
    if value.get("version") in (READ_BUDGET_VERSION, VERSION):
        fields.add("max_read_chars")
        if type(value.get("max_read_chars")) is not int or value["max_read_chars"] != 9600:
            raise ContractError("invalid_source_tool_contract")
    if (
        manifest.get("behavior_profile") != "production/1"
        or value.get("version") not in (VERSION, READ_BUDGET_VERSION, FINAL_TURN_VERSION, LEGACY_VERSION)
        or set(value) != fields
    ):
        raise ContractError("unsupported_source_tool_contract")
    if (
        not isinstance(value["definitions"], list)
        or not value["definitions"]
        or any(
            type(value[key]) is not int or value[key] <= 0 for key in ("max_model_turns", "max_tool_calls")
        )
    ):
        raise ContractError("invalid_source_tool_contract")
    names = []
    for tool in value["definitions"]:
        if (
            not isinstance(tool, dict)
            or tool.get("type") != "function"
            or not isinstance(tool.get("name"), str)
            or not isinstance(tool.get("parameters"), dict)
        ):
            raise ContractError("invalid_source_tool_contract")
        names.append(tool["name"])
    if len(set(names)) != len(names):
        raise ContractError("invalid_source_tool_contract")
    return value


def applies(stage, contract):
    return contract is not None and base_stage(stage) in ROLES


def bind(body, stage, profile, contract):
    if applies(stage, contract):
        body.update(tools=deepcopy(contract["definitions"]), tool_choice="auto", parallel_tool_calls=False)
        if profile.provider == "openai":
            body["include"] = ["reasoning.encrypted_content"]
    return body
