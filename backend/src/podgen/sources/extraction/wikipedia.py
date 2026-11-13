"""Legacy ingestion wikipedia helpers, operating on saved content."""

import re
from urllib.parse import unquote, urlparse

_WIKI_SECTION_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
_WIKI_TIMESTAMP_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")
_WIKIPEDIA_NAMESPACE_PREFIXES = {
    "category",
    "file",
    "help",
    "media",
    "mediawiki",
    "module",
    "portal",
    "special",
    "talk",
    "template",
    "template talk",
    "user",
    "user talk",
    "wikipedia",
    "wikipedia talk",
}


def _parse_wikipedia_page(url: str) -> tuple[str, str] | None:
    """Return (host, page_slug) for article URLs like /wiki/Foo_Bar."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "wikipedia.org" and not host.endswith(".wikipedia.org"):
        return None

    path = parsed.path or ""
    if not path.startswith("/wiki/"):
        return None

    slug = unquote(path[len("/wiki/") :].strip("/")).replace(" ", "_")
    if not slug:
        return None

    # Exclude known namespace pages (Category:, File:, Talk:, etc.),
    # but keep valid article slugs that contain ":" in the title.
    prefix, sep, _ = slug.partition(":")
    if sep and prefix.strip().lower().replace("_", " ") in _WIKIPEDIA_NAMESPACE_PREFIXES:
        return None

    return host, slug


def _wikipedia_sections_to_markdown(text: str) -> str:
    """Convert MediaWiki heading markers (== ... ==) to markdown for chunking."""

    def _replace(match: re.Match[str]) -> str:
        marker = match.group(1)
        heading = match.group(2).strip()
        if not heading:
            return ""
        level = len(marker)
        md_level = 2 if level == 2 else 3
        return f"{'#' * md_level} {heading}"

    return _WIKI_SECTION_RE.sub(_replace, text)


def _extract_wikipedia_revision_date(page_obj: dict) -> str | None:
    """Extract YYYY-MM-DD from the first revision timestamp, if present."""
    revisions = page_obj.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        return None

    first = revisions[0]
    if not isinstance(first, dict):
        return None

    timestamp = first.get("timestamp")
    if not isinstance(timestamp, str):
        return None

    match = _WIKI_TIMESTAMP_DATE_RE.match(timestamp)
    if not match:
        return None
    return match.group(1)
