"""Freeze the research admission, projection and reading adapter independently of releases."""

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.research.materials.contracts import VERSION, is_research
from podgen.research.materials.sections import SECTIONING

from .instructions import VERSION as INSTRUCTIONS
from .schemas import VERSION as SCHEMA

KEY = "research_material_contract"
LEGACY_PROJECTION = "research-section-views/1"
PROJECTION = "research-section-views/2"


def pin(materials, *, projection=PROJECTION):
    if not is_research(getattr(materials, "packet", None)):
        return None
    return dict(
        version=VERSION,
        sectioning=SECTIONING,
        projection=projection,
        instructions=INSTRUCTIONS,
        schema=SCHEMA,
        input_ref=materials.shared_ref.model_dump(mode="json"),
        packet_hash=digest(materials.packet.model_dump(mode="json")),
    )


def validate(materials, manifest):
    saved = manifest.get(KEY)
    projection = saved.get("projection") if isinstance(saved, dict) else PROJECTION
    if projection not in (LEGACY_PROJECTION, PROJECTION):
        raise ContractError("unsupported_research_material_projection")
    expected = pin(materials, projection=projection)
    if manifest.get(KEY) != expected:
        raise ContractError("research_material_contract_binding_mismatch")
    tools = manifest.get("source_tool_contract")
    tool_version = tools.get("version") if isinstance(tools, dict) else None
    if expected is None and tool_version in (
        "frozen-corpus-tools/5",
        "frozen-corpus-tools/6",
        "frozen-corpus-tools/7",
        "frozen-corpus-tools/8",
    ):
        raise ContractError("unexpected_research_source_tool_contract")
    if expected is not None and (
        (projection == PROJECTION) != (tool_version in ("frozen-corpus-tools/7", "frozen-corpus-tools/8"))
    ):
        raise ContractError("research_runtime_projection_mismatch")
    if expected is not None and tool_version not in (
        "frozen-corpus-tools/5",
        "frozen-corpus-tools/6",
        "frozen-corpus-tools/7",
        "frozen-corpus-tools/8",
    ):
        raise ContractError("research_source_tool_contract_missing")


def instructions(materials, role, body):
    if is_research(getattr(materials, "packet", None)):
        from .instructions import adapt

        return adapt(role, materials.format, body)
    return body


def role_instructions(materials, role, behavior):
    sources = "research" if is_research(getattr(materials, "packet", None)) else "cards"
    if sources == "cards":
        return behavior.instructions(role, materials.format)
    return behavior.instructions(role, materials.format, sources=sources)
