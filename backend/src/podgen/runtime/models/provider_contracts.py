"""Freeze provider API selection separately from the installed SDK version."""

from podgen.domain.errors import Conflict

from .messages_versions import api_version

KEY = "provider_contracts"


def contract(profile):
    result = dict(
        provider=profile.provider, api_family=profile.api_family, adapter_version=profile.adapter_version
    )
    if profile.provider == "anthropic":
        result["api_version"] = api_version(profile.adapter_version)
    return result


def validate(manifest, stage, profile):
    if KEY not in manifest:
        return  # Historical profiles already pin their original adapter; never upgrade it by inference.
    if manifest[KEY].get(stage) != contract(profile):
        raise Conflict("provider_contract_binding_mismatch")
