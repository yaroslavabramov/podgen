"""Pin the source-only role beside its original instructions, never infer a fresh role from text."""

import hashlib

from podgen.domain.errors import ContractError
from podgen.writing.prompts.composition import identity

KEY = "fact_source_instruction_variants"


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pin(behavior, role, format, original):
    variant = behavior.instructions(role, format, sources="sources")
    pinned = dict(
        body=variant.body,
        sha256=fingerprint(variant.body),
        source_sha256=fingerprint(original),
        version=identity(role, variant.body),
    )
    resolve(pinned, original)
    return pinned


def resolve(variant, original):
    if variant is None:
        from .fact_source_instructions import adapt

        return adapt(original)
    if (
        not isinstance(variant, dict)
        or set(variant) != {"body", "sha256", "source_sha256", "version"}
        or not isinstance(variant["body"], str)
        or not variant["body"].strip()
        or fingerprint(variant["body"]) != variant["sha256"]
        or fingerprint(original) not in (variant["source_sha256"], variant["sha256"])
        or variant["version"]
        not in (identity("fact-review", variant["body"]), identity("fact-verification", variant["body"]))
    ):
        raise ContractError("fact_source_instruction_variant_binding_mismatch")
    return variant["body"]


def from_manifest(manifest, stage="fact-review"):
    if KEY not in manifest:
        return None
    variants = manifest[KEY]
    if not isinstance(variants, dict) or stage not in variants:
        raise ContractError("fact_source_instruction_variant_missing")
    return variants[stage]


def for_request(manifest, stage):
    from .fact_source_mode import KEY as MODE_KEY

    if not (manifest.get("source_tool_contract") or {}).get(MODE_KEY):
        return None
    return from_manifest(manifest, stage)
