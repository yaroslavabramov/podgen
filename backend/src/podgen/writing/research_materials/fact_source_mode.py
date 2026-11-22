"""An explicit fact-review input variant, never a production default or a new evidence meaning."""

import json
from copy import deepcopy

from podgen.domain.errors import ContractError

KEY = "fact_review_input_mode"
VERSION = "retained-sources-only/1"
ALLOWED_TOOLS = frozenset(
    {
        "search_sources",
        "read_source",
        "list_sources",
        "request_source",
        "save_working_state",
        "list_work_history",
        "read_work_history",
    }
)
HIDDEN_FIELDS = {"research_contents", "research_results", "supplemental_research"}


def validate(mode):
    if mode not in (None, VERSION):
        raise ContractError("unsupported_fact_review_input_mode")
    return mode


def tools(definitions):
    result = [deepcopy(t) for t in definitions if t.get("name") in ALLOWED_TOOLS]
    if {t["name"] for t in result} != ALLOWED_TOOLS:
        raise ContractError("fact_source_only_tool_baseline_mismatch")
    for tool in result:
        if tool["name"] == "search_sources":
            tool["description"] = tool["description"].replace(
                "S identifiers match the report's final Sources numbers and may be sparse; source_catalog also lists unavailable sources. An M scope searches readable documents explicitly cited by that report section, not verified support.",
                "S identifiers may be sparse; source_catalog also lists unavailable sources. An S scope searches that retained document; null searches all readable retained source documents.",
            )
            tool["parameters"]["properties"]["within"]["description"] = (
                "Optional S identifier from source_catalog or search_sources. S restricts the search to that retained document; null searches all readable retained source documents."
            )
        if tool["name"] == "read_source":
            field = tool["parameters"]["properties"]["source_id"]
            field["description"] = field["description"].replace(", not a report section", "")
        if tool["name"] == "save_working_state":
            field = tool["parameters"]["properties"]["receipt_ids"]
            field["description"] = field["description"].replace("E/R", "E")
        if tool["name"] in ("list_work_history", "read_work_history"):
            tool["description"] = tool["description"].replace("E/R", "E")
    result.extend(deepcopy(t) for t in definitions if t.get("type") == "web_search")
    return result


def schema(value):
    result = deepcopy(value)
    fields = result.get("$defs", {}).get("SourcedFactFinding", {}).get("properties", {})
    if "support_labels" not in fields:
        raise ContractError("fact_source_only_schema_baseline_mismatch")
    labels = fields["support_labels"]
    labels["description"] = (
        "Return [] because no report material is supplied. Factual support remains in source_read_ids and the audit source_refs."
    )
    labels["items"].pop("enum", None)
    fields["source_read_ids"]["description"] = fields["source_read_ids"]["description"].replace(
        " R report-reading receipts are not source evidence.", ""
    )
    return result


def request(body, mode, *, source_instruction_variant=None):
    if validate(mode) is None:
        return body
    if body["metadata"]["podgen_stage"] != "fact-review":
        raise ContractError("fact_source_only_requires_initial_fact_review")
    from .source_instruction_variant import resolve

    result = deepcopy(body)
    result["instructions"] = resolve(source_instruction_variant, result["instructions"])
    result["tools"] = tools(result["tools"])
    result["text"]["format"]["schema"] = schema(result["text"]["format"]["schema"])
    for message in result["input"]:
        if message.get("role") != "user":
            continue
        for part in message["content"]:
            if part.get("type") != "input_text":
                raise ContractError("fact_source_only_payload_baseline_mismatch")
            payload = json.loads(part["text"])
            for field in HIDDEN_FIELDS:
                payload.pop(field, None)
            part["text"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return result


def initialize(corpus, materials, source_type):
    loaded = {doc.path: doc for doc in materials.documents}
    corpus.research_materials = materials
    corpus.research_sources = {row.source_id: row for row in materials.packet.sources}
    corpus.research_sections = {}
    corpus.research_texts = {}
    specs = {doc.key: doc for doc in materials.packet.documents if doc.purpose == "primary"}
    for source in materials.packet.sources:
        if source.document in specs and source.status in ("available", "partial"):
            spec = specs[source.document]
            corpus.documents[source.source_id] = source_type(
                source.title, loaded[spec.source_path].text, spec.sha256
            )
