from app.ingestion.chunker import TARGET_MAX_TOKENS, _RawChunk, _count_tokens, _split_large_chunk


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
