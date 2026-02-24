from app.ingestion.web_quality import assess_web_fetch_health, clean_web_text, resolve_web_title


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


def test_resolve_web_title_falls_back_to_heading() -> None:
    text = (
        "N/A\n"
        "# History of Note-Issuing Banks in Hong Kong\n"
        "Some content below."
    )

    title = resolve_web_title(None, text=text, url="https://www.hkma.gov.hk/eng/foo/bar/")

    assert title == "History of Note-Issuing Banks in Hong Kong"
