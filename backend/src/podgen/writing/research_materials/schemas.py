"""Research-only evidence descriptions; public fields and validation remain unchanged."""

from copy import deepcopy

from podgen.domain.errors import ContractError
from podgen.research.materials.contracts import is_research

VERSION = "research-evidence-descriptions/1"
DESCRIPTIONS = {
    "support_labels": "Exact offered material IDs locating the report sections relevant to this finding; these references are not source evidence.",
    "source_read_ids": "Exact successful current E-read aliases or retained H-evidence aliases supporting this finding and its correction. Empty only when the necessary source evidence is already supplied in the current packet. R report-reading receipts are not source evidence.",
}


def adapt(materials, stage, schema):
    if not is_research(getattr(materials, "packet", None)) or not (
        stage == "fact-review" or stage.startswith("fact-review-")
    ):
        return schema
    result = deepcopy(schema)
    properties = result.get("$defs", {}).get("SourcedFactFinding", {}).get("properties", {})
    if not set(DESCRIPTIONS) <= set(properties):
        raise ContractError("research_fact_schema_baseline_mismatch")
    for field, description in DESCRIPTIONS.items():
        properties[field]["description"] = description
    return result
