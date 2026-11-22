"""Bounded reading of immutable supplemental reports; citations remain discovery leads."""

import re

from podgen.research.hosted.material import http_url, links

from .sections import CITATION, source_mapping

KEY = "targeted_report_delivery"
LEGACY = "retained-report-navigation/1"
VERSION = "retained-report-navigation/2"
VERSIONS = (LEGACY, VERSION)
EXCERPT_CHARS = 2400


def validate(value):
    from podgen.domain.errors import ContractError

    if value not in (None, *VERSIONS):
        raise ContractError("unsupported_targeted_report_delivery")
    return value


def model_result(result):
    if result.get(KEY) not in VERSIONS:
        return result
    return {
        key: value
        for key, value in result.items()
        if key not in {KEY, "research_order_id", "validation_contract", "reported_source_count"}
    }


def reported_links(text, material):
    """Never infer an original URL from a redirect token, domain or source title."""
    from podgen.domain.errors import ContractError

    try:
        numbered = source_mapping(text)
    except ContractError as error:
        if error.code != "research_ambiguous_markdown_source_number":
            raise
        numbered = {}  # Preserve links without asserting an ambiguous number association.
    rows = {}
    for number, item in numbered.items():
        row = rows.setdefault(
            item["url"], dict(reported_url=item["url"], title=item["label"], report_numbers=[])
        )
        row["report_numbers"].append(number)
    urls = [row["url"] for row in material.get("reported_sources", [])]
    urls.extend(url for url, _, _ in links(text))
    for url in urls:
        if http_url(url):
            rows.setdefault(url, dict(reported_url=url, title=url, report_numbers=[]))
    return list(rows.values())


def source_links(mapping, materials):
    """Only sources in this active material scope can receive a readable S identifier."""
    known = [
        (s.source_id, s.title, s.reported_url, s.resolved_url, s.status)
        for s in materials.packet.sources
        if s.document
    ]
    known.extend(
        (
            s.source_id,
            s.title,
            s.catalog.get("reported_url"),
            s.catalog.get("resolved_url"),
            s.catalog.get("status", "available"),
        )
        for s in materials.runtime_admissions
        if s.catalog.get("kind") != "research_report"
    )
    from podgen.domain.errors import ContractError
    from podgen.research.hosted.corpus_acquisition import document_url

    def canonical(url):
        try:
            return document_url(url) if url else None
        except (ValueError, ContractError):
            return None

    by_url = {}
    for source in known:
        for url in source[2:4]:
            if (key := canonical(url)) is not None:
                by_url.setdefault(key, source)
    result = []
    for row in mapping:
        match = by_url.get(canonical(row["reported_url"]))
        if match:
            identity, title, _, resolved, status = match
            result.append(
                dict(
                    row,
                    source_id=identity,
                    title=title,
                    availability=status,
                    **(dict(resolved_url=resolved) if resolved else {}),
                    open=dict(tool="read_source", source_id=identity, start=0, end=None),
                )
            )
        else:
            result.append(
                dict(
                    row,
                    source_id=None,
                    availability="not_acquired",
                    open=dict(
                        tool="request_source",
                        url=row["reported_url"],
                        reason="Inspect this reported original source.",
                    ),
                )
            )
    return result


def excerpt_range(text, question):
    """Select an exact local context window; the entire report remains available."""
    words = set(re.findall(r"\w{4,}", question.casefold()))
    bibliography = re.search(r"^(?:\*\*Sources:\*\*|#{1,6}\s+Sources:?)\s*$", text, re.M)
    limit = bibliography.start() if bibliography else len(text)
    paragraphs = list(re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text[:limit]))
    if not paragraphs:
        return 0, min(len(text), EXCERPT_CHARS)
    chosen = max(paragraphs, key=lambda p: len(words & set(re.findall(r"\w{4,}", p[0].casefold()))))
    return chosen.start(), min(limit, chosen.start() + EXCERPT_CHARS)


def passage_sources(text, mapping):
    numbers = {int(n) for match in CITATION.finditer(text) for n in match[1].replace(",", " ").split()}
    urls = {url for url, _, _ in links(text)}
    return [
        row
        for row in mapping
        if numbers.intersection(row.get("report_numbers", [])) or row["reported_url"] in urls
    ][:6]


def reading_links(result_id, start, end):
    return dict(
        read_report=dict(tool="read_research_result", result_id=result_id, start=start, end=end),
        source_catalog=dict(tool="list_research_sources", result_id=result_id, offset=0, version=None),
    )
