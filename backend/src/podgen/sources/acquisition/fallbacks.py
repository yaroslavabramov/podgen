"""Related acquisitions are explicit documents, never replacements for saved bytes."""

import os
from urllib.parse import quote

from podgen.sources.extraction.jina import (
    _find_pdf_candidate_url_from_markdown,
    _is_navigation_heavy_jina_text,
)
from podgen.sources.extraction.wikipedia import _parse_wikipedia_page


def mediawiki_url(url):
    page = _parse_wikipedia_page(url)
    if page is None:
        return None
    host, title = page
    return (
        f"https://{host}/w/api.php?action=query&format=json&formatversion=2"
        "&prop=extracts|revisions&redirects=1&explaintext=1&exsectionformat=wiki"
        "&rvprop=timestamp&rvlimit=1&rvdir=newer&titles=" + quote(title, safe="")
    )


def jina_request(url, profile):
    key = os.environ.get(profile.jina_credential_env)
    if not key:
        return None
    return "https://r.jina.ai/" + url, {
        "Authorization": "Bearer " + key,
        "Accept": "text/markdown",
        "X-Return-Format": "markdown",
    }


def promoted_pdf(text, url):
    return (
        _find_pdf_candidate_url_from_markdown(text, base_url=url)
        if _is_navigation_heavy_jina_text(text)
        else None
    )
