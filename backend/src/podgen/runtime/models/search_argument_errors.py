"""Pinned recovery for rejected search continuation arguments, never index failures."""

KEY = "search_argument_policy"
VERSION = "recoverable-search-result-version/1"
PAGING = (
    " For the next page, keep query and within exactly unchanged from the search "
    "that returned result_version; copy query verbatim and use its next_offset. "
    "A rephrased query or changed within is a new search: set result_version=null "
    "and offset=0 instead of reusing the previous key."
)


def result(arguments):
    retry = dict(arguments, result_version=None, offset=0)
    if "aliases_of" in retry:
        retry["aliases_of"] = None
    return dict(
        error="source_search_result_version_mismatch",
        next_action=(
            "This result_version does not belong to the requested query and scope. "
            "Start search_sources for the intended query and within with result_version=null "
            "and offset=0. Reuse only the result_version returned for that exact search. "
            "No results from the rejected version were returned."
        ),
        retry_arguments=retry,
    )
