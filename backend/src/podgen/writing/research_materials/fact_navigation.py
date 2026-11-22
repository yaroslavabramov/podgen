"""Opt-in factual navigation; historical commissions keep their exact context."""

KEY = "fact_research_navigation"
VERSION = "shown-source-passages/1"


def attach(body):
    from podgen.writing.prompts.composition import read

    return body + read("research-navigation.md")


def enabled(contract, stage):
    return bool(
        contract
        and contract.get(KEY) == VERSION
        and (stage == "fact-review" or stage.startswith("fact-review-"))
        and contract.get("fact_review_input_mode") is None
    )


def history_enabled(contract, stage):
    """Source-only work has source/search history, but never the report catalog."""
    from .runtime_tools import ROLE_TOOL_KEY, ROLE_TOOL_VERSION

    return bool(
        contract
        and contract.get(KEY) == VERSION
        and (stage == "fact-review" or stage.startswith("fact-review-"))
        and (
            contract.get("fact_review_input_mode") is None or contract.get(ROLE_TOOL_KEY) == ROLE_TOOL_VERSION
        )
    )
