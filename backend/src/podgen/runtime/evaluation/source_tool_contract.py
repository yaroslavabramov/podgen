"""Versioned orchestration and frozen tool declarations; no inference for historical manifests."""

from copy import deepcopy

from podgen.domain.errors import ContractError
from podgen.runtime.models import web_search
from podgen.runtime.models.search_argument_errors import KEY as SEARCH_ARGUMENT_KEY
from podgen.runtime.models.search_argument_errors import PAGING as SEARCH_PAGING
from podgen.runtime.models.search_argument_errors import VERSION as SEARCH_ARGUMENT_VERSION

from .reading_workflow import base_stage

KEY = "source_tool_contract"
LEGACY_VERSION = "frozen-corpus-tools/1"
FINAL_TURN_VERSION = "frozen-corpus-tools/2"
READ_BUDGET_VERSION = "frozen-corpus-tools/3"
VERSION = "frozen-corpus-tools/4"
RESEARCH_VERSION = "frozen-corpus-tools/5"
PAGED_RESEARCH_VERSION = "frozen-corpus-tools/6"
DURABLE_RESEARCH_VERSION = "frozen-corpus-tools/7"
CURRENT_RESEARCH_VERSION = "frozen-corpus-tools/8"
DURABLE_VERSIONS = (DURABLE_RESEARCH_VERSION, CURRENT_RESEARCH_VERSION)
RESEARCH_VERSIONS = (RESEARCH_VERSION, PAGED_RESEARCH_VERSION, *DURABLE_VERSIONS)
RESEARCH_ROLES = (
    "plan",
    "plan-review",
    "plan-revise",
    "plan-review-revised",
    "draft",
    "revise",
    "fact-review",
)
ROLES = ("draft", "revise", "fact-review")


def create(behavior, materials=None):
    research = (
        materials is not None and getattr(materials.packet, "contract", None) == "research-editorial-input/1"
    )
    if research and behavior.name != "production/1":
        raise ContractError("research_materials_require_production_behavior")
    if behavior.name != "production/1":
        return None
    from podgen.retrieval.config import profile
    from podgen.writing.source_tools import definitions

    retrieval = profile()
    declarations = definitions(retrieval=True, retrieval_policy=retrieval.get("retrieval_policy"))
    if research:
        from podgen.writing.research_materials.tools import definitions as research_definitions

        declarations = research_definitions(declarations)
        from podgen.retrieval.config import streaming_profile
        from podgen.runtime.models.research_history_view import VERSION as history_view
        from podgen.writing.research_materials.runtime_tools import definitions as runtime_definitions
        from podgen.writing.research_materials.runtime_tools import source_definitions

        return dict(
            version=CURRENT_RESEARCH_VERSION,
            **{SEARCH_ARGUMENT_KEY: SEARCH_ARGUMENT_VERSION, web_search.KEY: web_search.CURRENT},
            fact_request_research=targeted_definition(history_view),
            web_search_definition=dict(type="web_search", search_context_size="low"),
            definitions=source_definitions(declarations, retrieval_policy=retrieval.get("retrieval_policy"))
            + runtime_definitions(history_view=history_view),
            retrieval=streaming_profile(),
            max_read_chars=9600,
            default_read_chars=9600,
            context_policy="durable-model-window/1",
            history_view=history_view,
        )
    return dict(
        **({"default_read_chars": 9600} if research else {}),
        version=PAGED_RESEARCH_VERSION if research else VERSION,
        definitions=declarations,
        retrieval=retrieval,
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
    if value.get("version") in DURABLE_VERSIONS:
        return validate_durable(manifest, value)
    fields = {"version", "definitions", "max_model_turns", "max_tool_calls"}
    if value.get("version") in (FINAL_TURN_VERSION, READ_BUDGET_VERSION, VERSION, *RESEARCH_VERSIONS):
        fields.add("final_turn")
        if value.get("final_turn") != "structured_without_tools":
            raise ContractError("invalid_source_tool_contract")
    if value.get("version") in (VERSION, *RESEARCH_VERSIONS):
        from podgen.retrieval.config import validate

        fields.add("retrieval")
        validate(value.get("retrieval"))
    if value.get("version") in (READ_BUDGET_VERSION, VERSION, *RESEARCH_VERSIONS):
        fields.add("max_read_chars")
        if type(value.get("max_read_chars")) is not int or value["max_read_chars"] != 9600:
            raise ContractError("invalid_source_tool_contract")
    if value.get("version") == PAGED_RESEARCH_VERSION:
        fields.add("default_read_chars")
        if type(value.get("default_read_chars")) is not int or value["default_read_chars"] != 9600:
            raise ContractError("invalid_source_tool_contract")
    if (
        manifest.get("behavior_profile") != "production/1"
        or value.get("version")
        not in (*RESEARCH_VERSIONS, VERSION, READ_BUDGET_VERSION, FINAL_TURN_VERSION, LEGACY_VERSION)
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
    if value["version"] in RESEARCH_VERSIONS and set(names) != {
        "search_sources",
        "read_source",
        "read_material",
    }:
        raise ContractError("invalid_source_tool_contract")
    return value


def applies(stage, contract):
    if contract is None:
        return False
    roles = RESEARCH_ROLES if contract["version"] in RESEARCH_VERSIONS else ROLES
    return base_stage(stage) in roles


def bind(body, stage, profile, contract):
    if applies(stage, contract):
        body.update(
            tools=role_definitions(contract, stage, profile.provider),
            tool_choice="auto",
            parallel_tool_calls=False,
        )
        if contract["version"] in DURABLE_VERSIONS:
            body["metadata"]["podgen_context_policy"] = contract["context_policy"]
        if profile.provider == "openai":
            body["include"] = ["reasoning.encrypted_content"]
            if quick_search(contract, stage, profile.provider):
                body["include"].append("web_search_call.action.sources")
    return body


def validate_durable(manifest, value):
    from podgen.retrieval.config import STREAMING_PROFILE, streaming_profile
    from podgen.writing.research_materials.runtime_tools import definitions as runtime_definitions
    from podgen.writing.research_materials.runtime_tools import source_definitions

    fields = {"version", "definitions", "retrieval", "max_read_chars", "default_read_chars", "context_policy"}
    if value["version"] == CURRENT_RESEARCH_VERSION:
        fields.update({"fact_request_research", "web_search_definition"})
    from podgen.writing.research_materials.fact_source_mode import KEY, VERSION, tools

    if web_search.KEY in value:
        if value["version"] != CURRENT_RESEARCH_VERSION or value[web_search.KEY] != web_search.CURRENT:
            raise ContractError("unsupported_web_search_receipt_contract")
        fields.add(web_search.KEY)
    if SEARCH_ARGUMENT_KEY in value:
        if (
            value["version"] != CURRENT_RESEARCH_VERSION
            or value[SEARCH_ARGUMENT_KEY] != SEARCH_ARGUMENT_VERSION
        ):
            raise ContractError("unsupported_search_argument_policy")
        fields.add(SEARCH_ARGUMENT_KEY)
    mode = value.get(KEY)
    if KEY in value:
        if mode != VERSION or manifest.get(KEY) != mode or manifest.get("stages") != ["fact-review"]:
            raise ContractError("invalid_fact_review_input_mode")
        fields.add(KEY)
    history_view = value.get("history_view")
    if "history_view" in value:
        if history_view != "research-work-history/1":
            raise ContractError("unsupported_research_history_view")
        fields.add("history_view")

    if (
        manifest.get("behavior_profile") != "production/1"
        or set(value) != fields
        or value["retrieval"] not in (STREAMING_PROFILE, streaming_profile())
        or value["context_policy"] != "durable-model-window/1"
        or type(value["max_read_chars"]) is not int
        or value["max_read_chars"] != 9600
        or type(value["default_read_chars"]) is not int
        or value["default_read_chars"] != 9600
    ):
        raise ContractError("unsupported_durable_source_tool_contract")
    from podgen.writing.research_materials.tools import definitions as research_definitions
    from podgen.writing.source_tools import definitions

    policy = value["retrieval"].get("retrieval_policy")
    expected = source_definitions(
        research_definitions(definitions(retrieval=True, retrieval_policy=policy)), retrieval_policy=policy
    ) + runtime_definitions(history_view=history_view)
    if mode is not None:
        expected = tools(expected)
    if value["version"] == CURRENT_RESEARCH_VERSION and (
        value.get("fact_request_research") != targeted_definition(history_view)
        or value.get("web_search_definition") != dict(type="web_search", search_context_size="low")
    ):
        raise ContractError("fact_search_tool_definitions_changed")
    if value["definitions"] != expected:
        raise ContractError("durable_source_tool_definitions_changed")
    return value


def instructions(body, stage, contract):
    if contract is not None and contract["version"] in DURABLE_VERSIONS and applies(stage, contract):
        from podgen.writing.research_materials.runtime_instructions import attach

        return attach(body, anchored=anchored_research(contract, stage))
    return body


def anchored_research(contract, stage):
    return contract["version"] == CURRENT_RESEARCH_VERSION and base_stage(stage) == "fact-review"


def quick_search(contract, stage, provider):
    return (
        contract["version"] == CURRENT_RESEARCH_VERSION
        and base_stage(stage) == "fact-review"
        and provider == "openai"
    )


def role_definitions(contract, stage, provider):
    result = deepcopy(contract["definitions"])
    if anchored_research(contract, stage):
        result = [
            deepcopy(contract["fact_request_research"]) if t["name"] == "request_research" else t
            for t in result
        ]
    if anchored_research(contract, stage) and contract.get(SEARCH_ARGUMENT_KEY) == SEARCH_ARGUMENT_VERSION:
        search = next(tool for tool in result if tool["name"] == "search_sources")
        search["description"] += SEARCH_PAGING
    if quick_search(contract, stage, provider):
        result.append(deepcopy(contract["web_search_definition"]))
    return result


def targeted_definition(history_view):
    from podgen.writing.research_materials.runtime_tools import definitions

    return next(
        t for t in definitions(history_view=history_view, anchored=True) if t["name"] == "request_research"
    )
