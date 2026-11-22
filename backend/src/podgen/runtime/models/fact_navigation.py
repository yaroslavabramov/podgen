"""Exact, assessment-scoped delivered search previews; never evidence receipts."""

from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.writing.research_materials.fact_navigation import VERSION


def annotate(result, corpus, history):
    if "hits" not in result:
        return result, None
    result = dict(result, hits=[deepcopy(hit) for hit in result["hits"]])
    identities = []
    for hit in result["hits"]:
        source = getattr(corpus, "documents", {}).get(hit.get("source_id"))
        start, end, text = hit.get("preview_start"), hit.get("preview_end"), hit.get("preview")
        if (
            source is None
            or not isinstance(text, str)
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end
            or end - start != len(text)
        ):
            continue  # No trustworthy exact preview locator: never guess that it was seen.
        identity = digest(dict(document=source.digest, start=hit["start"], end=hit["end"]))
        preview = digest(dict(document=source.digest, start=start, end=end, text=text))
        shown = history.shown_search_previews.get(preview)
        returned = history.returned_search_passages.get(identity)
        previous = shown if shown is not None else returned
        identities.append((identity, preview))
        if previous is not None:
            hit["previous_result"] = dict(
                index=previous, match="same_text" if shown is not None else "same_location_new_excerpt"
            )
    return result, dict(contract=VERSION, passages=identities)


def entry_catalog(materials):
    from .research_catalog import report_catalog

    page = report_catalog(materials, dict(offset=0, version=None))
    result = dict(
        reports=[dict(result_id=row["result_id"], question=row["title"]) for row in page["reports"]]
    )
    if page["next_offset"] is not None:
        result["next_page"] = dict(
            tool="list_research_results", offset=page["next_offset"], version=page["catalog_version"]
        )
    return result


def source_result(result):
    """Acquisition references remain in the receipt, not the researcher's conversation."""
    if result.get("status") != "unavailable":
        return result
    diagnostics = list(result.get("diagnostics") or [])
    for representation in result.get("representations") or []:
        diagnostics.extend(representation.get("diagnostics") or [])
    reason = next((d for d in diagnostics if isinstance(d, str) and d.startswith("http_status_")), None)
    reason = reason or next((d for d in diagnostics if isinstance(d, str)), "source_not_available")
    return dict(status="unavailable", url=result.get("url"), reason=reason)


def history_page(text, start, end):
    from .research_tools import PAGE, text_page

    result = text_page(text, start, end)
    if "error" not in result:
        return result
    if type(start) is int and 0 <= start < len(text):
        stop = min(len(text), start + PAGE)
        return dict(
            result,
            max_page_characters=PAGE,
            message="Read at most 9600 characters per page; end=null selects one page. Follow next_start from each successful page.",
            retry=dict(start=start, end=stop),
        )
    return dict(
        result,
        max_page_characters=PAGE,
        message="Start must be within the stored text. Use start=0, end=null for the first page.",
        retry=dict(start=0, end=None),
    )
