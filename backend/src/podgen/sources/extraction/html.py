"""Legacy ingestion html helpers, operating on saved content."""

import re
from urllib.parse import unquote, urlparse

_PDF_URL_HINT_RE = re.compile(r"\.pdf(?:$|[?#])|/downloadpdf/", re.IGNORECASE)


def _strip_html(html: str) -> tuple[str, str | None]:
    """Minimal HTML to text conversion using the stdlib html.parser."""
    import re
    from html.parser import HTMLParser

    title: str | None = None

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []
            self._in_title = False
            self._skip_tags = {"script", "style", "nav", "footer", "header", "aside"}
            self._skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in self._skip_tags:
                self._skip_depth += 1
            if tag == "title":
                self._in_title = True

        def handle_endtag(self, tag):
            if tag in self._skip_tags and self._skip_depth > 0:
                self._skip_depth -= 1
            if tag == "title":
                self._in_title = False
            if tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
                self.parts.append("\n")

        def handle_data(self, data):
            nonlocal title
            if self._in_title:
                title = data.strip()
            if self._skip_depth == 0:
                self.parts.append(data)

    extractor = _Extractor()
    extractor.feed(html)
    text = "".join(extractor.parts)

    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip(), title


def _looks_like_pdf_url(url: str) -> bool:
    return bool(_PDF_URL_HINT_RE.search(url))


def _title_from_pdf_url(url: str) -> str | None:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").split("/")[-1]
    if not slug:
        return None
    slug = re.sub(r"\.pdf$", "", unquote(slug), flags=re.IGNORECASE)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug or None
