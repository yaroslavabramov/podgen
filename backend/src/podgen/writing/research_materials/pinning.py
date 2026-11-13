"""Freeze the research admission, projection and reading adapter independently of releases."""

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.research.materials.contracts import VERSION, is_research
from podgen.research.materials.sections import SECTIONING

from .instructions import VERSION as INSTRUCTIONS
from .schemas import VERSION as SCHEMA

KEY = "research_material_contract"
PROJECTION = "research-section-views/1"


def pin(materials):
    if not is_research(getattr(materials, "packet", None)):
        return None
    return dict(
        version=VERSION,
        sectioning=SECTIONING,
        projection=PROJECTION,
        instructions=INSTRUCTIONS,
        schema=SCHEMA,
        input_ref=materials.shared_ref.model_dump(mode="json"),
        packet_hash=digest(materials.packet.model_dump(mode="json")),
    )


def validate(materials, manifest):
    expected = pin(materials)
    if manifest.get(KEY) != expected:
        raise ContractError("research_material_contract_binding_mismatch")
    tools = manifest.get("source_tool_contract")
    tool_version = tools.get("version") if isinstance(tools, dict) else None
    if expected is None and tool_version == "frozen-corpus-tools/5":
        raise ContractError("unexpected_research_source_tool_contract")
    if expected is not None and tool_version != "frozen-corpus-tools/5":
        raise ContractError("research_source_tool_contract_missing")


def instructions(materials, role, body):
    if is_research(getattr(materials, "packet", None)):
        from .instructions import adapt

        return adapt(role, materials.format, body)
    return body
