from app.ingestion.web_quality import (
    assess_web_fetch_health,
    clean_web_text,
    resolve_web_title,
    resolve_web_title_detailed,
)


def test_gate_a_blocks_captcha_pages() -> None:
    text = (
        "Warning: Target URL returned error 403: Forbidden\n"
        "This page maybe requiring CAPTCHA.\n"
        "Just a moment...\n"
        "Performing security verification.\n"
    )

    assessment = assess_web_fetch_health(text, min_chars=160)

    assert assessment.state == "blocked"
    assert "captcha_detected" in assessment.reasons


def test_gate_a_does_not_block_plain_forbidden_word() -> None:
    text = (
        "This article discusses the Forbidden City and why some ideas were forbidden in history. "
        "It does not describe an HTTP error page."
    )

    assessment = assess_web_fetch_health(text, min_chars=50)

    assert assessment.state == "ok"
    assert "http_403_error" not in assessment.reasons


def test_gate_a_blocks_short_http_403_error_banner() -> None:
    text = "Warning: Target URL returned error 403: Forbidden"

    assessment = assess_web_fetch_health(text, min_chars=50)

    assert assessment.state == "blocked"
    assert "http_403_error" in assessment.reasons


def test_gate_a_blocks_jina_http_error_pages() -> None:
    text = (
        "Title: Error\n\n"
        "Warning: Target URL returned error 400: Bad Request\n\n"
        "ERROR: This is an invalid URL. Please reenter the URL."
    )

    assessment = assess_web_fetch_health(text, min_chars=50)

    assert assessment.state == "blocked"
    assert "http_error_banner" in assessment.reasons
    assert "invalid_url_error_page" in assessment.reasons


def test_gate_a_marks_short_text_as_low_signal() -> None:
    assessment = assess_web_fetch_health("Home\nSkip to content\n", min_chars=160)

    assert assessment.state == "low_signal"
    assert "too_short" in assessment.reasons


def test_clean_web_text_removes_markdown_noise() -> None:
    raw = (
        "[Skip to content](https://example.com/#main)\n"
        "## Main Article Title\n"
        "![Image 1](https://example.com/logo.jpg)\n"
        "Read [this section](https://example.com/section) for details.\n"
        "\n\n\n"
        "Home\n"
    )

    cleaned = clean_web_text(raw)

    assert "Skip to content" not in cleaned
    assert "https://example.com/section" not in cleaned
    assert "Main Article Title" in cleaned
    assert "this section" in cleaned


def test_clean_web_text_removes_nav_and_url_lines() -> None:
    """Nav lines, bare URLs, and dividers are removed; content is preserved."""
    raw = (
        "Home\n"
        "Menu\n"
        "---\n"
        "## Article Title\n"
        "https://example.com/some/tracking/link\n"
        "This is meaningful content about the topic.\n"
    )

    cleaned = clean_web_text(raw)

    assert "Home" not in cleaned
    assert "Menu" not in cleaned
    assert "---" not in cleaned
    assert "https://example.com" not in cleaned
    assert "Article Title" in cleaned
    assert "meaningful content" in cleaned


def test_clean_web_text_preserves_setext_heading_structure() -> None:
    raw = (
        "Evolution of the Colonial Sterling Exchange Standard\n"
        "===============\n"
        "This section contains substantive content.\n"
    )

    cleaned = clean_web_text(raw)

    assert "Evolution of the Colonial Sterling Exchange Standard" in cleaned
    assert "===============" in cleaned
    assert "substantive content" in cleaned


def test_clean_web_text_removes_empty_links_and_service_lines() -> None:
    raw = (
        "Published Time: 1951-01-01\n"
        "Loading [MathJax]/extensions/MathMenu.js\n"
        "[](https://example.com/track)\n"
        "The article body starts here.\n"
    )

    cleaned = clean_web_text(raw)

    assert "Published Time:" not in cleaned
    assert "Loading [MathJax]" not in cleaned
    assert "https://example.com/track" not in cleaned
    assert "article body starts here" in cleaned.lower()


def test_clean_web_text_keeps_normal_bullet_sentence() -> None:
    raw = (
        "* A practical explanation of the linked exchange rate system is provided below.\n"
        "This paragraph continues with concrete details.\n"
    )

    cleaned = clean_web_text(raw)

    assert "practical explanation of the linked exchange rate system" in cleaned.lower()
    assert "paragraph continues" in cleaned.lower()


def test_clean_web_text_removes_bullet_nav_and_js_placeholder_lines() -> None:
    raw = (
        "* [Skip to Main content](https://example.com/#main)\n"
        "* [Home](https://example.com/)\n"
        "[](javascript:void(0);)\n"
        "The article body starts here with real content.\n"
    )

    cleaned = clean_web_text(raw)

    assert "Skip to Main content" not in cleaned
    assert "Home" not in cleaned
    assert "javascript:void(0)" not in cleaned
    assert "article body starts here" in cleaned.lower()


def test_resolve_web_title_falls_back_to_heading() -> None:
    text = (
        "N/A\n"
        "# History of Note-Issuing Banks in Hong Kong\n"
        "Some content below."
    )

    title = resolve_web_title(None, text=text, url="https://www.hkma.gov.hk/eng/foo/bar/")

    assert title == "History of Note-Issuing Banks in Hong Kong"


def test_resolve_web_title_prefers_setext_over_generic_heading() -> None:
    text = (
        "# Topics\n"
        "Evolution of the Colonial Sterling Exchange Standard\n"
        "====================================================\n"
        "Body content.\n"
    )

    title = resolve_web_title(None, text=text, url="https://www.elibrary.imf.org/view/...")

    assert title == "Evolution of the Colonial Sterling Exchange Standard"


def test_resolve_web_title_keeps_good_extracted_title() -> None:
    text = (
        "# Topics\n"
        "Open external link\n"
        "Body text.\n"
    )

    title = resolve_web_title(
        "Hong Kong Monetary Authority",
        text=text,
        url="https://en.wikipedia.org/wiki/Hong_Kong_Monetary_Authority",
    )

    assert title == "Hong Kong Monetary Authority"


def test_resolve_web_title_drops_generic_extracted_title() -> None:
    text = (
        "Lessons from history - HSBC History\n"
        "===================================\n"
        "Body text.\n"
    )

    title, meta = resolve_web_title_detailed(
        "Open external link",
        text=text,
        url="https://history.hsbc.com/history/lessons",
    )

    assert title == "Lessons from history - HSBC History"
    assert meta["title_source"] == "setext_heading"
