"""Only newly pinned diversity policies expose grouped alias pagination."""

from pydantic import Field

from podgen.retrieval.config import PASSAGE_POLICY

from .contracts import Search


class DiverseSearch(Search):
    aliases_of: int | None = Field(
        default=None,
        ge=0,
        description="Null for ranked hits; use next_alias_page arguments to read all original selectors of a grouped passage. Offset then pages aliases, not ranked hits.",
    )


def schema(model):
    value = model.model_json_schema()
    value["required"] = list(value["properties"])
    value["properties"]["aliases_of"].pop("default", None)
    return value


def bind(tools, policy):
    if policy == PASSAGE_POLICY:
        search = next(tool for tool in tools if tool["name"] == "search_sources")
        extended = schema(DiverseSearch)
        for name, field in search["parameters"]["properties"].items():
            extended["properties"][name] = field
        search["parameters"] = extended
        search["description"] += (
            " Shared original text is grouped, not independent confirmation. Overlapping text variants are not equivalent: inspect their exact differences and both original contexts. Aliases are original window selectors, not claims of exact equivalence. Use next_alias_page to inspect all original source selectors."
        )
    return tools
