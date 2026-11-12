"""Compile existing canonical voice/model profiles to the same execution port used in trials."""

from podgen.domain.contracts.configuration import ModelProfile, VoiceProfile
from podgen.domain.errors import ContractError

from .contracts import SynthesisProfile


def compile_profile(voice: VoiceProfile, model: ModelProfile, *, sample_rate_hz=24000, output_format="wav"):
    if voice.provider != model.provider or model.fallback_profile_refs:
        raise ContractError("synthesis_canonical_profile_provider_or_fallback_mismatch")
    if "synthesis" not in model.allowed_roles or not any(
        locale.startswith("ru") for locale in voice.language_locales
    ):
        raise ContractError("synthesis_canonical_profile_role_or_language_mismatch")
    if voice.voice_kind == "authorized_custom" and voice.authorization_ref is None:
        raise ContractError("custom_synthesis_voice_requires_authorization")
    return SynthesisProfile(
        provider=model.provider,
        model_id=model.model_revision_or_alias,
        voice_id=voice.provider_voice_id,
        sample_rate_hz=sample_rate_hz,
        output_format=output_format,
        options=model.generation_parameters,
    )
