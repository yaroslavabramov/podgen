from app.ingestion.chunker import (
    TARGET_MAX_TOKENS,
    _has_supporting_body,
    _infer_heading_level,
    _normalize_sizes,
    _RawChunk,
    _count_tokens,
    _pack_segments,
    _split_by_headers,
    _split_large_chunk,
)


def test_split_large_chunk_splits_oversized_single_paragraph() -> None:
    content = ("token " * (TARGET_MAX_TOKENS + 900)).strip()
    chunk = _RawChunk(content=content, heading_path=["Section"], token_count=_count_tokens(content))

    pieces = _split_large_chunk(chunk)

    assert len(pieces) > 1
    assert all(piece.token_count <= TARGET_MAX_TOKENS for piece in pieces)
    assert all(piece.heading_path == ["Section"] for piece in pieces)


def test_split_large_chunk_splits_flat_text_with_single_newlines() -> None:
    line = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    content = "\n".join([line for _ in range(700)])
    chunk = _RawChunk(content=content, heading_path=[], token_count=_count_tokens(content))

    pieces = _split_large_chunk(chunk)

    assert len(pieces) > 1
    assert all(piece.token_count <= TARGET_MAX_TOKENS for piece in pieces)


def test_split_by_headers_infers_outline_structure_without_markdown() -> None:
    text = (
        "Preamble sentence about the document.\n"
        "1. Background\n"
        "This section introduces context and key assumptions for the topic.\n"
        "It contains enough words to be considered real body content.\n"
        "1.1 Monetary Framework\n"
        "Detailed mechanics are explained here with concrete examples.\n"
        "Additional explanatory details follow in the same section.\n"
        "2. Outcomes\n"
        "Final section summarises the outcomes and implications.\n"
    )

    chunks = _split_by_headers(text)

    assert len(chunks) >= 3
    assert any(chunk.heading_path == ["1. Background"] for chunk in chunks)
    assert any(chunk.heading_path in (["1. Background", "1.1 Monetary Framework"], ["1.1 Monetary Framework"]) for chunk in chunks)
    assert any(chunk.heading_path == ["2. Outcomes"] for chunk in chunks)


def test_split_by_headers_parses_setext_markdown_headings() -> None:
    text = (
        "Document intro paragraph with enough meaningful words for preamble.\n\n"
        "Main Title\n"
        "==========\n"
        "This first section describes the core context in a concise way.\n\n"
        "Abstract\n"
        "--------\n"
        "This abstract section captures the framing and high-level argument.\n"
    )

    chunks = _split_by_headers(text)

    assert any(chunk.heading_path == ["Main Title"] for chunk in chunks)
    assert any(chunk.heading_path and chunk.heading_path[-1] == "Abstract" for chunk in chunks)


def test_split_by_headers_parses_all_markdown_heading_levels() -> None:
    text = (
        "# Root\n"
        "Root context text with enough words to represent real body content.\n\n"
        "#### Deep Section\n"
        "Nested section body with specific detail and supporting explanation.\n"
    )

    chunks = _split_by_headers(text)

    assert any(chunk.heading_path == ["Root"] for chunk in chunks)
    assert any(chunk.heading_path == ["Root", "Deep Section"] for chunk in chunks)


def test_split_by_headers_parses_cyrillic_markdown_headings() -> None:
    text = (
        "# Введение\n"
        "Краткий контекст и постановка проблемы.\n\n"
        "## Детали\n"
        "Подробное объяснение механизма с примерами и выводами.\n"
    )

    chunks = _split_by_headers(text)

    assert any(chunk.heading_path == ["Введение"] for chunk in chunks)
    assert any(chunk.heading_path == ["Введение", "Детали"] for chunk in chunks)


def test_split_by_headers_ignores_toc_like_heading_lists() -> None:
    text = (
        "Contents\n"
        "Chapter I General Principles\n"
        "Chapter II Institutional Design\n"
        "Chapter III Implementation\n"
        "\n"
        "This document explains the policy in detail and provides substantial body text.\n"
        "More body text follows with analysis and operational details.\n"
    )

    chunks = _split_by_headers(text)

    assert len(chunks) == 1
    assert chunks[0].heading_path == []


def test_pack_segments_never_exceeds_target_tokens_on_join_boundary() -> None:
    import random
    import string

    random.seed(42)
    segments: list[str] = []
    # Reproduce a deterministic corpus that previously caused > TARGET_MAX_TOKENS.
    for trial in range(13):
        n = random.randint(120, 420)
        current: list[str] = []
        for _ in range(n):
            wlen = random.randint(1, 8)
            word = "".join(random.choice(string.ascii_letters) for _ in range(wlen))
            punct = random.choice([".", "!", "?", ":"])
            current.append(word + punct)
        if trial == 12:
            segments = current

    packed = _pack_segments(segments)

    assert packed
    assert max(_count_tokens(part) for part in packed) <= TARGET_MAX_TOKENS


def test_normalize_sizes_does_not_merge_chunks_with_different_heading_path() -> None:
    left_text = "alpha " * 40
    right_text = "beta " * 40
    left = _RawChunk(content=left_text.strip(), heading_path=["Chapter I"], token_count=_count_tokens(left_text))
    right = _RawChunk(content=right_text.strip(), heading_path=["Chapter II"], token_count=_count_tokens(right_text))

    normalized = _normalize_sizes([left, right])

    assert len(normalized) == 2
    assert normalized[0].heading_path == ["Chapter I"]
    assert normalized[1].heading_path == ["Chapter II"]


def test_normalize_sizes_merges_chunks_with_same_heading_path() -> None:
    left_text = "alpha " * 40
    right_text = "beta " * 40
    left = _RawChunk(content=left_text.strip(), heading_path=["Chapter I"], token_count=_count_tokens(left_text))
    right = _RawChunk(content=right_text.strip(), heading_path=["Chapter I"], token_count=_count_tokens(right_text))

    normalized = _normalize_sizes([left, right])

    assert len(normalized) == 1
    assert normalized[0].heading_path == ["Chapter I"]


def test_split_by_headers_nested_chapters_get_correct_heading_path() -> None:
    """Chapter II sections must not inherit Chapter I's heading."""
    text = (
        "Preamble with enough words to count as real body content here.\n"
        "This document establishes the fundamental legal framework for governance.\n"
        "\n"
        "Chapter I General Principles\n"
        "Article 1 The region shall implement autonomy with sufficient detail.\n"
        "Article 2 The national laws shall apply with enough context words.\n"
        "Article 3 The government shall protect the rights and freedoms of all.\n"
        "Article 4 The legislature shall enact laws in accordance with this text.\n"
        "\n"
        "Chapter II Fundamental Rights\n"
        "Section 1 Civil Rights\n"
        "Article 24 Residents shall have freedom and many other fundamental rights.\n"
        "Article 25 All residents are equal before the law in every respect.\n"
        "Article 26 Every resident shall have the right to confidential legal advice.\n"
        "\n"
        "Section 2 Political Rights\n"
        "Article 27 Permanent residents shall have the right to vote freely.\n"
        "Article 28 Every resident shall have freedom of speech and of the press.\n"
        "Article 29 The right to peaceful assembly shall be guaranteed by law.\n"
    )

    chunks = _split_by_headers(text)

    assert len(chunks) >= 3

    for chunk in chunks:
        if "Section 1 Civil Rights" in " ".join(chunk.heading_path):
            assert chunk.heading_path[0] == "Chapter II Fundamental Rights", (
                f"Section 1 should be under Chapter II, got {chunk.heading_path}"
            )
        if "Section 2 Political Rights" in " ".join(chunk.heading_path):
            assert chunk.heading_path[0] == "Chapter II Fundamental Rights", (
                f"Section 2 should be under Chapter II, got {chunk.heading_path}"
            )


def test_has_supporting_body_transparent_child_headings() -> None:
    """A parent heading followed by child headings then body should be supported."""
    lines = [
        "Chapter IV Political Structure",   # 0 - level 2 candidate
        "Section 1 The Chief Executive",     # 1 - level 3 candidate
        "The Chief Executive shall be the head of the region and accountable to it.",  # 2 - body
    ]
    candidates_by_idx = {0: 2, 1: 3}

    # Chapter IV (level 2) should see through Section 1 (level 3) to the body
    assert _has_supporting_body(0, lines, candidates_by_idx, current_level=2) is True


def test_has_supporting_body_toc_chapter_rejected() -> None:
    """A TOC entry (Chapter followed by sibling Chapter, no body) should be rejected."""
    lines = [
        "Chapter I General Principles",   # 0
        "Chapter II Institutions",         # 1
        "Chapter III Implementation",      # 2
    ]
    candidates_by_idx = {0: 2, 1: 2, 2: 2}

    # Chapter I → next is Chapter II (sibling, same level) → no body → False
    assert _has_supporting_body(0, lines, candidates_by_idx, current_level=2) is False


def test_article_lines_not_detected_as_headings() -> None:
    """Article lines should not be treated as structural headings."""
    text = (
        "Preamble with enough words to be real body content for the document.\n"
        "\n"
        "Chapter I General Principles\n"
        "Article 1 The region shall be an inalienable part of the country.\n"
        "Article 2 The National Congress authorizes the region to exercise autonomy.\n"
        "Article 3 The executive authorities and legislature shall be composed of residents.\n"
        "\n"
        "Chapter II Relationship Between Central and Local\n"
        "Article 13 The Central Government shall be responsible for foreign affairs.\n"
        "Article 14 The Central Government shall be responsible for the defence.\n"
        "Article 15 The Central Government shall be responsible for appointments.\n"
    )

    chunks = _split_by_headers(text)

    # Articles should NOT appear in heading_path
    for chunk in chunks:
        for h in chunk.heading_path:
            assert not h.lower().startswith("article"), (
                f"Article should not be in heading_path: {chunk.heading_path}"
            )


def test_article_lines_rejected_by_generic_heuristic() -> None:
    """Article lines must not pass the capitalized-words heuristic either."""
    # Exact repro from review finding round 44
    assert _infer_heading_level("Article 1 Scope") is None
    assert _infer_heading_level("Article 2 Definitions") is None
    assert _infer_heading_level("Article 48 Powers and Functions") is None
    # But structural labels still work
    assert _infer_heading_level("Chapter IV Political Structure") == 2
    assert _infer_heading_level("Section 1 The Chief Executive") == 3
