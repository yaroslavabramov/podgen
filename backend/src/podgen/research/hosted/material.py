"""Lossless report blocks and reported links; no invented EvidenceSpan bindings."""

import re
from urllib.parse import urlsplit

from podgen.domain.errors import ContractError


def http_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def links(text):
    for match in re.finditer(r"https?://[^\s<>\[\]\"']+", text):
        candidate = match.group().rstrip(".,;:!?")
        while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        if http_url(candidate):
            yield candidate, match.start(), match.start() + len(candidate)


def report(value):
    steps = value.get("steps", [])
    if not isinstance(steps, list):
        raise ContractError("hosted_research_invalid_steps")
    outputs = [(i, step) for i, step in enumerate(steps) if step.get("type") == "model_output"]
    if not outputs:
        raise ContractError("hosted_research_completed_without_report")
    parts, blocks, sources, cursor = [], [], [], 0
    for step_index, output in outputs:
        for index, content in enumerate(output.get("content", [])):
            if content.get("type") != "text":
                continue
            text = content.get("text")
            if not isinstance(text, str):
                raise ContractError("hosted_research_invalid_report_text")
            if parts:
                cursor += 2
            path = ["steps", step_index, "content", index]
            blocks.append(
                dict(
                    provider_path=path,
                    report_start=cursor,
                    report_end=cursor + len(text),
                    annotations=content.get("annotations", []),
                )
            )
            for annotation in content.get("annotations", []):
                if annotation.get("type") == "url_citation" and http_url(annotation.get("url")):
                    sources.append(
                        dict(
                            url=annotation["url"],
                            origin="provider_annotation",
                            provider_path=path,
                            annotation=annotation,
                            evidence_status="reported_only",
                        )
                    )
            for url, first, last in links(text):
                sources.append(
                    dict(
                        url=url,
                        origin="report_text",
                        report_range=[cursor + first, cursor + last],
                        evidence_status="reported_only",
                    )
                )
            parts.append(text)
            cursor += len(text)
    text = "\n\n".join(parts)
    if not text.strip():
        raise ContractError("hosted_research_completed_without_report")
    return dict(
        text=text,
        blocks=blocks,
        reported_sources=sources,
        source_list_scope="links_reported_by_provider_not_complete_reading_history",
        annotation_offsets="provider_native_unvalidated",
    )
