"""Published strict packet shapes; one parser retains both historical and stable-ID packets."""

from .editorial_contract import EditorialInput, FixedEditorialInput


def packet_schema(*, version=2, fixed=False):
    if version not in (1, 2):
        raise ValueError("unknown_editorial_packet_version")
    model = FixedEditorialInput if fixed else EditorialInput
    schema = model.model_json_schema()
    contract = schema["properties"]["contract"]
    contract.pop("enum")
    contract["const"] = ("fixed-editorial-input/" if fixed else "editorial-input/") + str(version)
    if not fixed:
        card = schema["$defs"]["KnowledgeCard"]
        if version == 1:
            card["properties"].pop("card_id")
            card["properties"].pop("story_facts")
            card["properties"].pop("narrative_excerpt_indices")
        else:
            facts = card["properties"]["story_facts"]
            facts_schema = next(value for value in facts["anyOf"] if value.get("type") == "string")
            facts_schema["title"] = facts["title"]
            card["properties"]["story_facts"] = facts_schema
            selection = card["properties"]["narrative_excerpt_indices"]
            selection_schema = next(value for value in selection["anyOf"] if value.get("type") == "array")
            selection_schema["title"] = selection["title"]
            selection_schema["uniqueItems"] = True
            card["properties"]["narrative_excerpt_indices"] = selection_schema
            field = card["properties"]["card_id"]
            schema_field = next(value for value in field["anyOf"] if value.get("type") == "string")
            schema_field["title"] = field["title"]
            card["properties"]["card_id"] = schema_field
            card["required"].insert(0, "card_id")
    return schema
