"""Freeze provider API selection separately from the installed SDK version."""

from podgen.domain.errors import Conflict

from .messages_versions import api_version

KEY = "provider_contracts"
TOOL_POLICY = "messages_tool_policy"


def contract(profile):
    result = dict(
        provider=profile.provider, api_family=profile.api_family, adapter_version=profile.adapter_version
    )
    if profile.provider == "anthropic":
        result["api_version"] = api_version(profile.adapter_version)
        from .messages_tool_policy import VERSION

        result[TOOL_POLICY] = VERSION
    return result


def validate(manifest, stage, profile):
    if KEY not in manifest:
        return  # Historical profiles already pin their original adapter; never upgrade it by inference.
    saved, expected = manifest[KEY].get(stage), contract(profile)
    if saved is not None and TOOL_POLICY not in saved:
        expected.pop(TOOL_POLICY, None)
    if saved != expected:
        raise Conflict("provider_contract_binding_mismatch")


def tool_policy(manifest, stage):
    """Missing policy in an archived manifest selects the historical native wire."""
    return manifest.get(KEY, {}).get(stage, {}).get(TOOL_POLICY)
