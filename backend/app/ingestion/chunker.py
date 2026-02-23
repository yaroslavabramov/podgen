"""Structure-first text chunking with size normalization."""

import logging
import re
import uuid
from dataclasses import dataclass, field

import tiktoken
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk

logger = logging.getLogger(__name__)

# --- Constants -----------------------------------------------------------------
MIN_CHUNK_TOKENS = 128
TARGET_MIN_TOKENS = 256
TARGET_MAX_TOKENS = 1024
OVERLAP_TOKENS = 64

_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


# --- Internal data structure ---------------------------------------------------
@dataclass
class _RawChunk:
    content: str
    heading_path: list[str] = field(default_factory=list)
    token_count: int = 0


# --- Public API ----------------------------------------------------------------
async def structural_chunk(
    text: str,
    source_id: str,
    db: AsyncSession,
) -> list[Chunk]:
    """Structure-first chunking pipeline.

    Pass 1 -- Split by H2/H3 markdown headers, tracking heading_path.
    Pass 2 -- Normalize sizes: split large chunks (> TARGET_MAX_TOKENS) by
              paragraphs and merge small chunks (< MIN_CHUNK_TOKENS) with neighbours.
    Pass 3 -- Add OVERLAP_TOKENS-token overlap between consecutive chunks.
    Pass 4 -- Persist Chunk models to the database.

    Returns:
        List of Chunk ORM objects (already added to the session).
    """
    if not text or not text.strip():
        return []

    # Pass 1: structural split
    raw_chunks = _split_by_headers(text)
    logger.debug("Pass 1: %d structural chunks", len(raw_chunks))

    # Pass 2: normalize sizes
    normalized = _normalize_sizes(raw_chunks)
    logger.debug("Pass 2: %d normalized chunks", len(normalized))

    # Pass 3: add overlap
    with_overlap = _add_overlap(normalized)
    logger.debug("Pass 3: %d chunks with overlap", len(with_overlap))

    # Pass 4: persist
    db_chunks = _persist_chunks(with_overlap, source_id, db)
    db.add_all(db_chunks)
    await db.flush()

    logger.info("Chunked source %s: %d chunks", source_id, len(db_chunks))
    return db_chunks


# --- Pass 1: Split by headers -------------------------------------------------
_HEADER_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)


def _split_by_headers(text: str) -> list[_RawChunk]:
    """Split text on ## and ### boundaries, tracking a heading breadcrumb."""
    chunks: list[_RawChunk] = []
    heading_path: list[str] = []

    matches = list(_HEADER_RE.finditer(text))

    if not matches:
        # No headers: treat entire text as one chunk
        tc = _count_tokens(text)
        return [_RawChunk(content=text.strip(), heading_path=[], token_count=tc)]

    # Content before the first header
    preamble = text[: matches[0].start()].strip()
    if preamble:
        tc = _count_tokens(preamble)
        chunks.append(_RawChunk(content=preamble, heading_path=[], token_count=tc))

    for i, match in enumerate(matches):
        level = len(match.group(1))  # 2 or 3
        heading_text = match.group(2).strip()

        # Update heading breadcrumb
        if level == 2:
            heading_path = [heading_text]
        elif level == 3:
            # Keep the H2 parent, replace H3
            heading_path = heading_path[:1] + [heading_text]

        # Extract content between this header and the next (or end of text)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()

        if body:
            tc = _count_tokens(body)
            chunks.append(
                _RawChunk(
                    content=body,
                    heading_path=list(heading_path),
                    token_count=tc,
                )
            )

    return chunks


# --- Pass 2: Normalize sizes ---------------------------------------------------
def _normalize_sizes(chunks: list[_RawChunk]) -> list[_RawChunk]:
    """Split oversized chunks and merge undersized ones."""
    # Step A: split chunks that exceed TARGET_MAX_TOKENS
    expanded: list[_RawChunk] = []
    for chunk in chunks:
        if chunk.token_count > TARGET_MAX_TOKENS:
            expanded.extend(_split_large_chunk(chunk))
        else:
            expanded.append(chunk)

    # Step B: merge chunks smaller than MIN_CHUNK_TOKENS with neighbours
    merged: list[_RawChunk] = []
    for chunk in expanded:
        if (
            merged
            and chunk.token_count < MIN_CHUNK_TOKENS
            and merged[-1].token_count + chunk.token_count <= TARGET_MAX_TOKENS
        ):
            # Merge into previous chunk
            merged[-1].content += "\n\n" + chunk.content
            merged[-1].token_count = _count_tokens(merged[-1].content)
            # Keep the more specific heading path
            if len(chunk.heading_path) > len(merged[-1].heading_path):
                merged[-1].heading_path = chunk.heading_path
        elif (
            merged
            and merged[-1].token_count < MIN_CHUNK_TOKENS
            and merged[-1].token_count + chunk.token_count <= TARGET_MAX_TOKENS
        ):
            # Previous chunk is too small; merge current into it
            merged[-1].content += "\n\n" + chunk.content
            merged[-1].token_count = _count_tokens(merged[-1].content)
            if len(chunk.heading_path) > len(merged[-1].heading_path):
                merged[-1].heading_path = chunk.heading_path
        else:
            merged.append(chunk)

    return merged


def _split_large_chunk(chunk: _RawChunk) -> list[_RawChunk]:
    """Split a chunk that exceeds TARGET_MAX_TOKENS by paragraph boundaries."""
    paragraphs = re.split(r"\n{2,}", chunk.content)
    result: list[_RawChunk] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_tokens = _count_tokens(para)

        if current_tokens + para_tokens > TARGET_MAX_TOKENS and current_parts:
            result.append(
                _RawChunk(
                    content="\n\n".join(current_parts),
                    heading_path=list(chunk.heading_path),
                    token_count=current_tokens,
                )
            )
            current_parts = []
            current_tokens = 0

        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        result.append(
            _RawChunk(
                content="\n\n".join(current_parts),
                heading_path=list(chunk.heading_path),
                token_count=current_tokens,
            )
        )

    # If a single paragraph exceeds the limit we still return it (don't break mid-sentence)
    return result if result else [chunk]


# --- Pass 3: Add overlap ------------------------------------------------------
def _add_overlap(chunks: list[_RawChunk]) -> list[_RawChunk]:
    """Prepend the last OVERLAP_TOKENS tokens of the previous chunk to each chunk."""
    if len(chunks) <= 1:
        return chunks

    encoder = _get_encoder()
    result: list[_RawChunk] = [chunks[0]]

    for i in range(1, len(chunks)):
        prev_tokens = encoder.encode(chunks[i - 1].content)
        overlap_tokens = prev_tokens[-OVERLAP_TOKENS:] if len(prev_tokens) > OVERLAP_TOKENS else prev_tokens

        if overlap_tokens:
            overlap_text = encoder.decode(overlap_tokens)
            new_content = overlap_text.strip() + "\n\n" + chunks[i].content
            new_tc = _count_tokens(new_content)
        else:
            new_content = chunks[i].content
            new_tc = chunks[i].token_count

        result.append(
            _RawChunk(
                content=new_content,
                heading_path=list(chunks[i].heading_path),
                token_count=new_tc,
            )
        )

    return result


# --- Pass 4: Persist -----------------------------------------------------------
def _persist_chunks(
    raw_chunks: list[_RawChunk],
    source_id: str,
    db: AsyncSession,
) -> list[Chunk]:
    """Convert raw chunks into Chunk ORM objects."""
    db_chunks: list[Chunk] = []

    for idx, rc in enumerate(raw_chunks):
        chunk = Chunk(
            id=uuid.uuid4(),
            source_id=uuid.UUID(source_id),
            content=rc.content,
            heading_path=rc.heading_path,
            chunk_index=idx,
            token_count=rc.token_count,
        )
        db_chunks.append(chunk)

    return db_chunks
