from app.ingestion.chunker import (
    TARGET_MAX_TOKENS,
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
