"""Regressions ported from legacy ingestion, without legacy infrastructure."""

from podgen.sources.extraction.html import _looks_like_pdf_url
from podgen.sources.extraction.jina import (
    _extract_jina_title,
    _find_pdf_candidate_url_from_markdown,
    _is_navigation_heavy_jina_text,
)
from podgen.sources.extraction.wikipedia import (
    _extract_wikipedia_revision_date,
    _parse_wikipedia_page,
    _wikipedia_sections_to_markdown,
)


def test_parse_wikipedia_page_accepts_article_paths() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Hong_Kong_Monetary_Authority")
    assert parsed == ("en.wikipedia.org", "Hong_Kong_Monetary_Authority")


def test_parse_wikipedia_page_rejects_namespace_paths() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Category:Hong_Kong")
    assert parsed is None


def test_parse_wikipedia_page_rejects_non_wikipedia_domains() -> None:
    parsed = _parse_wikipedia_page("https://notwikipedia.org/wiki/Foo")
    assert parsed is None


def test_parse_wikipedia_page_rejects_encoded_namespace_paths() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Category%3AHong_Kong")
    assert parsed is None


def test_parse_wikipedia_page_accepts_articles_with_colon_in_slug() -> None:
    parsed = _parse_wikipedia_page("https://en.wikipedia.org/wiki/Star_Trek:_The_Next_Generation")
    assert parsed == ("en.wikipedia.org", "Star_Trek:_The_Next_Generation")


def test_extract_wikipedia_revision_date_from_first_revision() -> None:
    page_obj = {"revisions": [{"timestamp": "2004-04-26T12:34:56Z"}]}
    assert _extract_wikipedia_revision_date(page_obj) == "2004-04-26"


def test_extract_wikipedia_revision_date_returns_none_on_missing_revision() -> None:
    assert _extract_wikipedia_revision_date({}) is None
    assert _extract_wikipedia_revision_date({"revisions": []}) is None
    assert _extract_wikipedia_revision_date({"revisions": [{"timestamp": "invalid"}]}) is None


def test_extract_jina_title_uses_setext_heading_when_title_line_missing() -> None:
    payload = (
        "Published Time: 1951-01-01\n"
        "Open external link\n"
        "==================\n"
        "Evolution of the Colonial Sterling Exchange Standard\n"
        "====================================================\n"
        "Body starts here.\n"
    )

    assert _extract_jina_title(payload) == "Evolution of the Colonial Sterling Exchange Standard"


def test_extract_jina_title_uses_setext_when_title_line_is_na() -> None:
    payload = (
        "Title: N/A\nLessons from history - HSBC History\n===================================\nMain body.\n"
    )

    assert _extract_jina_title(payload) == "Lessons from history - HSBC History"


def test_extract_jina_title_keeps_explicit_valid_title() -> None:
    payload = "Title: Hong Kong Monetary Authority\n## Heading\nBody.\n"

    assert _extract_jina_title(payload) == "Hong Kong Monetary Authority"


def test_extract_jina_title_fallback_skips_raw_title_metadata_line() -> None:
    payload = "Title: N/A\nBody content starts here.\n"

    assert _extract_jina_title(payload) == "Body content starts here."


def test_extract_jina_title_allows_legit_search_prefixed_heading() -> None:
    payload = "Search Engine Market Structure\n==============================\nBody.\n"

    assert _extract_jina_title(payload) == "Search Engine Market Structure"


def test_wikipedia_sections_to_markdown_maps_levels() -> None:
    text = "Intro\n== History ==\nText\n=== Institutions ===\nMore"
    converted = _wikipedia_sections_to_markdown(text)
    assert "## History" in converted
    assert "### Institutions" in converted


def test_looks_like_pdf_url_recognizes_pdf_and_downloadpdf_paths() -> None:
    assert _looks_like_pdf_url("https://example.com/paper.pdf")
    assert _looks_like_pdf_url("https://example.com/downloadpdf/view/journals/abc")
    assert not _looks_like_pdf_url("https://example.com/article")


def test_find_pdf_candidate_url_from_markdown_same_host_only() -> None:
    text = (
        "[Download PDF](https://example.com/downloadpdf/paper.pdf)\n"
        "[Other PDF](https://evil.test/paper.pdf)\n"
    )
    found = _find_pdf_candidate_url_from_markdown(text, base_url="https://example.com/article")
    assert found == "https://example.com/downloadpdf/paper.pdf"


def test_navigation_heavy_jina_text_detects_portal_like_markdown() -> None:
    text = "\n".join(
        [
            "###### Topics",
            "###### Countries",
            "Advanced search",
        ]
        + [f"[Link {i}](https://example.com/{i})" for i in range(60)]
    )
    assert _is_navigation_heavy_jina_text(text) is True
