"""Explicit candidate configuration creation, without qualification or production approval."""

from uuid import uuid4, uuid5

from podgen.domain.contracts.common import CodeProducer
from podgen.domain.contracts.configuration import ModelProfile
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.compiler import COMPILER_VERSION, validate_profile
from podgen.runtime.models.messages_versions import CURRENT_ADAPTER


def create_profile(
    repo,
    project,
    *,
    model,
    provider="openai",
    context_limit,
    output_limit=65536,
    reasoning_effort="high",
    verbosity="high",
    timeout_seconds=1800,
    background=False,
    store=False,
    operation_id=None,
    roles=None,
):
    if provider not in {"openai", "anthropic"}:
        raise ContractError("unsupported_model_provider")
    if provider == "anthropic" and (background or store or reasoning_effort not in {"low", "medium", "high"}):
        raise ContractError("unsupported_messages_parameters")
    operation_id = operation_id or uuid4()
    producer = CodeProducer(
        kind="code",
        component=f"candidate-{provider}-profile",
        build_id=COMPILER_VERSION,
        operation_id=operation_id,
    )
    if background and not store:
        raise ValueError("background_requires_explicit_store")
    policy = repo.create(
        project,
        "Policy",
        dict(
            policy_kind="data",
            rules={
                "configuration_only": True,
                "external_provider": provider,
                "provider_response_storage": store,
                "app_archive_audience": "evaluator",
                "production_qualification": False,
            },
        ),
        producer=producer,
        operation_id=uuid5(operation_id, "data-policy"),
    )
    return repo.create(
        project,
        "ModelProfile",
        dict(
            provider=provider,
            api_family="messages" if provider == "anthropic" else "responses",
            model_id=model,
            model_revision_or_alias=model,
            adapter_version=CURRENT_ADAPTER if provider == "anthropic" else "openai-responses-v1",
            allowed_roles=roles if roles is not None else ["writer", "editorial_reviewer"],
            supported_modalities=["text"],
            context_limit=context_limit,
            output_limit=output_limit,
            structured_output_mode="json_schema",
            tool_call_mode="none",
            parallel_tool_policy="disabled",
            generation_parameters=dict(
                reasoning={"effort": reasoning_effort},
                text={"verbosity": verbosity},
                store=store,
                background=background,
            ),
            timeout_policy=dict(rpc_timeout_seconds=timeout_seconds, poll_deadline_seconds=timeout_seconds),
            data_retention_policy_ref=policy.ref.model_dump(mode="json"),
            caching_policy={"application_result_reuse": "same_invocation_only"},
            fallback_profile_refs=[],
            configured_languages=["ru"],
        ),
        producer=producer,
        operation_id=uuid5(operation_id, "model-profile"),
    )


def load_profile(generation, project, ref, stage):
    if ref.kind != "ModelProfile":
        raise ContractError("expected_model_profile")
    profile = ModelProfile.model_validate(generation.get(project, ref).payload)
    validate_profile(profile, stage)
    return profile
