from typing import Literal

from pydantic import JsonValue

from .common import UUID, ArtifactRef, Contract, PositiveInt, Text


class Policy(Contract):
    policy_kind: Literal["source", "quality", "audio", "data", "operations", "evaluation"]
    rules: dict[str, JsonValue]


class PolicyBundle(Contract):
    source_policy_ref: ArtifactRef
    quality_policy_ref: ArtifactRef
    audio_policy_ref: ArtifactRef
    data_policy_ref: ArtifactRef
    operations_policy_ref: ArtifactRef


class PromptDefinition(Contract):
    role: Text
    template: Text
    placeholders: list[str]
    examples: list[str]
    compiler_version: Text


class ToolDefinition(Contract):
    name: Text
    description: Text
    parameters: dict[str, JsonValue]
    permissions: list[str]


class ToolsetDefinition(Contract):
    tools: list[ToolDefinition]
    dispatcher_version: Text


class ModelProfile(Contract):
    provider: Text
    api_family: Text
    model_id: Text
    model_revision_or_alias: Text
    adapter_version: Text
    allowed_roles: list[str]
    supported_modalities: list[str]
    context_limit: PositiveInt
    output_limit: PositiveInt
    structured_output_mode: Text
    tool_call_mode: Text
    parallel_tool_policy: Text
    generation_parameters: dict[str, JsonValue]
    timeout_policy: dict[str, JsonValue]
    data_retention_policy_ref: ArtifactRef
    caching_policy: dict[str, JsonValue]
    fallback_profile_refs: list[ArtifactRef]
    configured_languages: list[str]


class ProcessingProfile(Contract):
    profile_kind: Literal[
        "extraction",
        "render",
        "recognition",
        "alignment",
        "assembly",
        "delivery",
        "media_decode",
        "calculation",
        "retrieval",
        "context",
        "evaluation",
    ]
    implementation: Text
    version: Text
    configuration: dict[str, JsonValue]
    input_profile_refs: list[ArtifactRef]


class CapabilityCheck(Contract):
    capability: Text
    expected: JsonValue
    observed: JsonValue
    outcome: Literal["pass", "fail", "inconclusive"]
    diagnostic_refs: list[ArtifactRef]


class CapabilityReport(Contract):
    profile_ref: ArtifactRef
    tested_configuration_fingerprint: Text
    adapter_version: Text
    checks: list[CapabilityCheck]
    observed_limits: dict[str, JsonValue]
    probe_run_ids: list[UUID]
    probe_invocation_ids: list[UUID]
    limitations: list[str]


class VoiceProfile(Contract):
    provider: Text
    provider_voice_id: Text
    provider_revision_or_alias: Text
    model_profile_ref: ArtifactRef
    language_locales: list[str]
    voice_kind: Literal["stock", "authorized_custom"]
    authorization_ref: ArtifactRef | None
    usage_policy_ref: ArtifactRef
    limitations: list[str]


class LexiconEntry(Contract):
    entry_id: UUID
    written_form: Text
    context_rule: str
    spoken_form: str | None
    phoneme_form: str | None
    phoneme_alphabet: str | None
    basis_refs: list[ArtifactRef]
    ambiguity_notes: list[str]


class PronunciationLexicon(Contract):
    language: Text
    entries: list[LexiconEntry]
    version_reason: Text


class DerivedPreview(Contract):
    derived_from_ref: ArtifactRef
    text: str
    coverage: Text
    compiler_version: Text


class DiagnosticArtifact(Contract):
    code: Text
    message: Text
    related_refs: list[ArtifactRef]
