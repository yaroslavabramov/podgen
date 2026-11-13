"""Operational research tools; editorial decisions remain in the existing role schema."""

from typing import Literal

from pydantic import Field

from podgen.writing.source_tools.contracts import Arguments, Search


class CatalogPage(Arguments):
    offset: int = Field(ge=0, description="Start at zero, then use next_offset.")


class SourceCatalogPage(CatalogPage):
    version: str | None = Field(
        description="Null on the first page; reuse returned catalog_version for continuation."
    )


class PersistentSearch(Search):
    result_version: str | None = Field(
        description="Null for a new search; reuse returned result_version with next_offset for exact persistent result pages, including after corpus expansion."
    )


class ResearchCitationPage(SourceCatalogPage):
    result_id: str = Field(
        description="X report identifier whose local citation numbers must be mapped to retained S documents."
    )


class SourceRequest(Arguments):
    source_id: str | None = Field(
        description="Known S identifier, or null when supplying a new URL. Supply exactly one of source_id and url."
    )
    url: str | None = Field(
        description="Exact new URL, or null when using source_id. Known S references avoid copying long redirect URLs."
    )
    reason: str


class ResearchRequest(Arguments):
    question: str
    reason: str


class WorkingState(Arguments):
    state: str = Field(
        description="Complete durable working result, progress, coverage, unresolved issues and next actions. No editorial verdict is required. This full text replaces no persisted evidence."
    )
    resume_state: str = Field(
        description="Separately authored compact continuation state, with exact pointers to full working notes and evidence. Full state is persisted without truncation; only this compact field enters the next model window."
    )
    receipt_ids: list[str] = Field(
        description="Exact E/R receipt IDs needed to resume. All other archived receipts remain available through work history."
    )


class HistoryRead(Arguments):
    kind: Literal["tool_result", "conversation"]
    index: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int | None


class ResearchRead(Arguments):
    result_id: str = Field(
        description="X identifier returned by request_research; synthesis, never primary evidence."
    )
    start: int = Field(ge=0)
    end: int | None


class ResearchSelection(Arguments):
    result_id: str = Field(
        description="X identifier of the full supplemental report selected for this plan's author."
    )
    reason: str = Field(description="Why the accepted plan's author needs this complete supplemental report.")


TOOLS = (
    (
        "list_research_results",
        SourceCatalogPage,
        "Discover supplemental X reports in pages, including their exact titles/questions. Reports are synthesis, not primary evidence.",
    ),
    (
        "list_research_sources",
        ResearchCitationPage,
        "Page the exact mapping from one X report's local citation numbers/URLs to retained S source IDs and availability. These links are not verified support.",
    ),
    (
        "select_research_result",
        ResearchSelection,
        "Planning roles only: explicitly select a full X report for the author if this exact candidate plan is accepted. Full selected reports accompany selected M sections. Reading alone does not select a report.",
    ),
    (
        "list_sources",
        SourceCatalogPage,
        "Page through the complete source catalog, including unavailable sources. Metadata is not source evidence.",
    ),
    (
        "request_source",
        SourceRequest,
        "Obtain a specific missing document during this task. A pending request pauses unfinished work; it does not create an editorial verdict.",
    ),
    (
        "request_research",
        ResearchRequest,
        "Return an unresolved research question to the research layer. Pending work resumes this same review before any author rewrite.",
    ),
    (
        "save_working_state",
        WorkingState,
        "Publish complete working state for a durable context continuation. Call this tool alone. Preserve reasoning needed for the task, findings so far, exact evidence references and unresolved work. This is an operational checkpoint, never a final review. All prior messages and raw reads remain archived and accessible.",
    ),
    (
        "list_work_history",
        CatalogPage,
        "Page through archived tool results and context segments. E/R identities remain the original delivered receipts.",
    ),
    (
        "read_work_history",
        HistoryRead,
        "Read exact archived tool-result or conversation JSON in character pages. This navigation creates no new evidence receipt; original E/R identifiers retain their meaning.",
    ),
    (
        "read_research_result",
        ResearchRead,
        "Read a full supplemental research report in pages. It creates R synthesis receipts, never E original-source evidence.",
    ),
)


def definitions(*, history_view=None):
    from podgen.domain.errors import ContractError

    if history_view not in (None, "research-work-history/1"):
        raise ContractError("unsupported_research_history_view")
    return [
        dict(
            type="function",
            name=name,
            strict=True,
            parameters=model.model_json_schema(),
            description=(
                "Read prior task messages, actions, arguments and results in character pages. "
                "Full working notes and exact source passages are retained; transport metadata is omitted. "
                "This navigation creates no new evidence receipt; original E/R identifiers retain their meaning."
                if name == "read_work_history" and history_view
                else description
            ),
        )
        for name, model, description in TOOLS
    ]


def source_definitions(original):
    from copy import deepcopy

    result = deepcopy(original)
    search = next(tool for tool in result if tool["name"] == "search_sources")
    schema = PersistentSearch.model_json_schema()
    for name in ("query", "within", "offset"):
        schema["properties"][name]["description"] = search["parameters"]["properties"][name]["description"]
    search["parameters"] = schema
    search["description"] += (
        " Pages enumerate a bounded ranked candidate pool from the complete corpus, not every possible match. "
        "result_scope and returned_candidates identify this scope; total_hits is the same ranked-candidate count. "
        "No hit or next_offset=null does not prove an account is absent. Reuse result_version with next_offset to preserve the result ordering."
    )
    return result
