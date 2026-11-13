"""Partition the complete Markdown deterministically; positions are Unicode code points."""

import re

from podgen.domain.errors import ContractError

from .contracts import ReportOccurrence, ReportSection

SECTIONING = "markdown-h1-h2-preserving-ranges/1"
SOURCE_LINE = re.compile(r"^([0-9]+)\.\s+\[([^\]]+)\]\((https?://[^\s)]+)\)\s*$", re.M)
CITATION = re.compile(r"\[cite:\s*([0-9 ,]+)\]")


def source_mapping(text):
    """Only explicit final Sources lines supply number→URL associations."""
    heading = list(re.finditer(r"^(?:\*\*Sources:\*\*|#{1,6}\s+Sources:?)\s*$", text, re.M))
    if not heading:
        return {}
    result = {}
    for match in SOURCE_LINE.finditer(text, heading[-1].end()):
        number = int(match[1])
        if number in result and result[number]["url"] != match[3]:
            raise ContractError("research_ambiguous_markdown_source_number")
        result[number] = dict(url=match[3], label=match[2], start=match.start(), end=match.end())
    return result


def partition(text, *, document="report", prefix="section", kind="research_report_section", sources=None):
    if not text.strip():
        raise ContractError("research_empty_document")
    starts, titles, parents = [0], {0: "Research report"}, {0: None}
    cursor, fence, parent = 0, None, None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker[1][0]
            fence = None if fence == token else token if fence is None else fence
        if fence is None:
            heading = re.match(r"^(#{1,2})\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
            bibliography = line.strip() == "**Sources:**"
            if heading or bibliography:
                title = heading[2] if heading else "Reported Sources"
                if heading and len(heading[1]) == 1:
                    parent = title
                if cursor not in titles:
                    starts.append(cursor)
                titles[cursor], parents[cursor] = title, parent
        cursor += len(line)
    starts = sorted(set(starts))
    result = []
    for index, (first, last) in enumerate(zip(starts, starts[1:] + [len(text)], strict=True), 1):
        if first == last:
            continue
        occurrences = []
        for match in CITATION.finditer(text, first, last):
            for number in map(int, match[1].replace(",", " ").split()):
                source = (sources or {}).get(number)
                if source:
                    occurrences.append(
                        ReportOccurrence(
                            start=match.start(),
                            end=match.end(),
                            reported_url=source["url"],
                            source_id=source["source_id"],
                        )
                    )
        result.append(
            ReportSection(
                material_id=f"{prefix}-{index:03d}",
                document=document,
                title=titles[first],
                start=first,
                end=last,
                kind=kind,
                parent_title=parents[first],
                occurrences=occurrences,
            )
        )
    if "".join(text[row.start : row.end] for row in result) != text:
        raise ContractError("research_section_partition_dropped_text")
    return result
