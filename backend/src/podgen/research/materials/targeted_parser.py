"""Accept one JSON answer and Gemini's source-list trailer, never free-form alternatives."""

import json
import re

from podgen.research.hosted.material import http_url
from podgen.runtime.evaluation.validation import load_unique_json


def answer_object(text):
    value = text.strip()
    fenced = re.match(r"```(?:json)?\s*\n", value)
    if fenced:
        value = value[fenced.end() :].lstrip()
    _, end = json.JSONDecoder().raw_decode(value)
    answer, trailer = value[:end], value[end:].strip()
    if fenced:
        if not trailer.startswith("```"):
            raise ValueError("targeted_research_unclosed_fence")
        trailer = trailer[3:].strip()
    if trailer:
        _source_list(trailer)
    return load_unique_json(answer)


def _source_list(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # This is the saved Gemini report appendix. Do not discard arbitrary prose
    # or a second answer merely because it follows a Sources heading.
    if lines[0] not in ("**Sources:**", "Sources:", "## Sources", "### Sources") or len(lines) < 2:
        raise ValueError("targeted_research_unexpected_trailer")
    for line in lines[1:]:
        match = re.fullmatch(r"([0-9]+)\. \[([^\[\]{}\r\n]+)\]\((https?://[^\s<>]+)\)", line)
        if match is None or not http_url(match[3]):
            raise ValueError("targeted_research_invalid_source_list")
