"""Full, text-only, self-contained requests. No filesystem, web, tools or conversation carry-over."""

import json
from copy import deepcopy
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.configuration import ModelProfile
from podgen.domain.errors import ContractError

COMPILER_VERSION = "whole-episode-baseline-a/1"
COUNT_FIELDS = {
    "model",
    "instructions",
    "input",
    "text",
    "reasoning",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "truncation",
}


def profile_role(stage):
    if stage in ("plan", "plan-revise"):
        return "planner"
    if stage in ("plan-review", "plan-review-revised"):
        return "plan_reviewer"
    return (
        "editorial_reviewer"
        if stage == "review" or stage.startswith(("reader-review", "fact-review"))
        else "writer"
    )


def validate_profile(profile: ModelProfile, stage):
    role = profile_role(stage)
    if (
        (profile.provider, profile.api_family, profile.adapter_version)
        not in {
            ("openai", "responses", "openai-responses-v1"),
            ("anthropic", "messages", "anthropic-messages-v1"),
        }
        or role not in profile.allowed_roles
        or profile.structured_output_mode != "json_schema"
        or profile.fallback_profile_refs
    ):
        raise ContractError("unsupported_evaluation_profile")
    if profile.output_limit >= profile.context_limit:
        raise ContractError("profile_output_exceeds_context")
    if profile.model_revision_or_alias == "gpt-5.4-2026-03-05" and (
        profile.context_limit > 1050000 or profile.output_limit > 128000
    ):
        raise ContractError("profile_exceeds_documented_model_capacity")
    allowed = {
        "reasoning",
        "text",
        "temperature",
        "top_p",
        "store",
        "background",
        "service_tier",
        "prompt_cache_retention",
    }
    parameters = profile.generation_parameters
    if not isinstance(parameters.get("text", {}), dict) or not isinstance(
        parameters.get("reasoning", {}), dict
    ):
        raise ContractError("invalid_generation_parameter_type")
    if type(parameters.get("store")) is not bool or type(parameters.get("background")) is not bool:
        raise ContractError("explicit_provider_storage_settings_required")
    if parameters.get("reasoning", {}).get("effort") not in ("none", "low", "medium", "high", "xhigh"):
        raise ContractError("invalid_reasoning_effort")
    if parameters.get("text", {}).get("verbosity") not in ("low", "medium", "high"):
        raise ContractError("invalid_text_verbosity")
    if any(
        type(profile.timeout_policy.get(name)) not in (int, float) or profile.timeout_policy[name] <= 0
        for name in ("rpc_timeout_seconds", "poll_deadline_seconds")
    ):
        raise ContractError("invalid_profile_timeout")
    if profile.provider == "anthropic" and (
        parameters["background"]
        or parameters["store"]
        or parameters["reasoning"]["effort"] not in {"low", "medium", "high"}
        or set(parameters) - {"store", "background", "reasoning", "text"}
    ):
        raise ContractError("unsupported_messages_parameters")
    if set(parameters) - allowed:
        raise ContractError("unsupported_generation_parameter")
    if set(parameters.get("text", {})) - {"verbosity"} or set(parameters.get("reasoning", {})) - {"effort"}:
        raise ContractError("unsupported_generation_parameter")
    if parameters.get("background") and not parameters.get("store"):
        raise ContractError("background_requires_explicit_store")
    if (
        profile.model_revision_or_alias.startswith("gpt-5.4")
        and parameters.get("reasoning", {}).get("effort", "none") != "none"
        and {"temperature", "top_p"} & set(parameters)
    ):
        raise ContractError("sampling_parameters_incompatible_with_reasoning")


def indexed_script(output):
    return dict(
        utterances=[dict(index=index, **utterance) for index, utterance in enumerate(output["utterances"])]
    )


def compile_request(
    run_id,
    stage,
    profile,
    materials,
    instructions,
    schema,
    *,
    output_language,
    draft=None,
    feedback=None,
    own_plan=None,
):
    validate_profile(profile, stage)
    payload = {"contract": "whole-episode-baseline-a/1", "stage": stage, "author_materials": materials}
    # Manifests created before the language field was introduced retain their original wire format.
    if output_language is not None:
        payload["output_language"] = output_language
    if stage in ("review", "revise"):
        if draft is None:
            raise ContractError("draft_required")
        payload["draft"] = indexed_script(draft)
    if stage == "revise":
        if feedback is None:
            raise ContractError("full_feedback_required")
        payload["editorial_feedback"] = deepcopy(feedback)
    if own_plan is not None:
        payload.update(own_plan)
    return response_request(run_id, stage, profile, payload, instructions, schema)


def response_request(run_id, stage, profile, payload, instructions, schema):
    validate_profile(profile, stage)
    parameters = deepcopy(profile.generation_parameters)
    text = parameters.pop("text", {})
    text["format"] = dict(
        type="json_schema",
        name="editorial_feedback"
        if stage == "review"
        else "episode_script"
        if stage in ("draft", "revise")
        else stage.replace("-", "_"),
        schema=deepcopy(schema),
        strict=True,
    )
    return dict(
        model=profile.model_revision_or_alias,
        instructions=instructions,
        input=[
            dict(
                role="user",
                content=[
                    dict(
                        type="input_text",
                        text=json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    )
                ],
            )
        ],
        max_output_tokens=profile.output_limit,
        text=text,
        tools=[],
        tool_choice="none",
        parallel_tool_calls=False,
        truncation="disabled",
        stream=False,
        metadata=dict(
            podgen_run_id=str(run_id),
            podgen_invocation_id=str(uuid5(run_id, "invocation:" + stage)),
            podgen_stage=stage,
            compiler_version=COMPILER_VERSION,
        ),
        **parameters,
    )


def materials_hash(materials):
    return digest(materials)
