"""Structure-first text chunking with size normalization."""

import logging
import re
import uuid
from dataclasses import dataclass, field

from markdown_it import MarkdownIt
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
_markdown_parser: MarkdownIt | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def _get_markdown_parser() -> MarkdownIt:
    global _markdown_parser
    if _markdown_parser is None:
        _markdown_parser = MarkdownIt("commonmark")
    return _markdown_parser


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

    Pass 1 -- Split by markdown headings (H1-H6, ATX + Setext), tracking heading_path.
              If no markdown heading structure is found, infer headings from plain text.
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
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_OUTLINE_PREFIX_RE = re.compile(
    r"^(?P<prefix>(?:\d+(?:\.\d+)+(?:[)\.:-])?|\d+[)\.:-]|(?:[IVXLCM]+|[A-Z])[)\.:-]))\s+(?P<title>.+)$"
)
_SECTION_LABEL_RE = re.compile(
    r"^(?P<label>chapter|section|part|annex|appendix|book|title)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9'/-]*")
_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_GENERIC_HEADING_WORDS = {"introduction", "background", "overview", "summary", "conclusion"}
_MIN_BODY_WORDS_FOR_HEADING = 6


def _split_by_headers(text: str) -> list[_RawChunk]:
    """Split text on markdown headings first, then plain-text inferred headings."""
    markdown_chunks = _split_by_markdown_headings(text)
    if markdown_chunks:
        return markdown_chunks

    inferred = _split_by_inferred_headings(text)
    if inferred:
        return inferred

    # No structure detected: treat entire text as one chunk.
    tc = _count_tokens(text)
    return [_RawChunk(content=text.strip(), heading_path=[], token_count=tc)]


def _split_by_markdown_headings(text: str) -> list[_RawChunk]:
    """Split text by markdown heading tokens (H1-H6; ATX and Setext)."""
    lines = text.splitlines()
    if not lines:
        return []

    parser = _get_markdown_parser()
    tokens = parser.parse(text)

    headings: list[dict] = []
    for idx, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        if not token.tag.startswith("h"):
            continue
        try:
            level = int(token.tag[1:])
        except (TypeError, ValueError):
            continue
        if level < 1 or level > 6:
            continue

        line_map = token.map or []
        if len(line_map) < 2:
            continue
        line_start = int(line_map[0])
        line_end = int(line_map[1])
        if line_start < 0 or line_end < line_start:
            continue

        heading_text = ""
        if idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
            heading_text = (tokens[idx + 1].content or "").strip()
        if not _is_supported_markdown_heading_text(heading_text):
            continue

        headings.append(
            {
                "level": level,
                "text": heading_text,
                "line_start": line_start,
                "line_end": line_end,
            }
        )

    if not headings:
        return []

    headings.sort(key=lambda item: int(item["line_start"]))
    deduped: list[dict] = []
    seen_starts: set[int] = set()
    for heading in headings:
        line_start = int(heading["line_start"])
        if line_start in seen_starts:
            continue
        seen_starts.add(line_start)
        deduped.append(heading)
    headings = deduped

    chunks: list[_RawChunk] = []
    first_start = int(headings[0]["line_start"])
    preamble = "\n".join(lines[:first_start]).strip()
    if preamble:
        chunks.append(_RawChunk(content=preamble, heading_path=[], token_count=_count_tokens(preamble)))

    heading_path: list[str] = []
    for i, heading in enumerate(headings):
        level = int(heading["level"])
        heading_text = str(heading["text"])
        heading_path = heading_path[: max(0, level - 1)] + [heading_text]

        start = int(heading["line_end"])
        end = int(headings[i + 1]["line_start"]) if i + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue

        chunks.append(
            _RawChunk(
                content=body,
                heading_path=list(heading_path),
                token_count=_count_tokens(body),
            )
        )

    return chunks


def _is_supported_markdown_heading_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < 2 or len(normalized) > 220:
        return False
    if _URL_ONLY_RE.match(normalized):
        return False
    return len(_WORD_RE.findall(normalized)) >= 1


def _split_by_inferred_headings(text: str) -> list[_RawChunk]:
    """Infer heading structure from plain text lines when markdown headers are absent."""
    lines = text.splitlines()
    inferred = _infer_heading_lines(lines)
    if not inferred:
        return []

    chunks: list[_RawChunk] = []
    first_idx = inferred[0]["line_index"]
    preamble = "\n".join(lines[:first_idx]).strip()
    if preamble:
        chunks.append(_RawChunk(content=preamble, heading_path=[], token_count=_count_tokens(preamble)))

    current_h2: str | None = None
    for i, heading in enumerate(inferred):
        level = int(heading["level"])
        heading_text = str(heading["text"]).strip()

        # Always update heading tracking, even if this heading has no body
        if level == 2:
            current_h2 = heading_text

        start = heading["line_index"] + 1
        end = inferred[i + 1]["line_index"] if i + 1 < len(inferred) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue

        if level == 2:
            heading_path = [heading_text]
        else:
            heading_path = [current_h2, heading_text] if current_h2 else [heading_text]

        chunks.append(
            _RawChunk(
                content=body,
                heading_path=heading_path,
                token_count=_count_tokens(body),
            )
        )

    return chunks


def _infer_heading_lines(lines: list[str]) -> list[dict]:
    """Return inferred heading lines with confidence/TOC guards."""
    non_empty_indexes = [idx for idx, line in enumerate(lines) if line.strip()]
    if len(non_empty_indexes) < 6:
        return []

    candidates: list[dict] = []
    for idx in non_empty_indexes:
        line = lines[idx].strip()
        level = _infer_heading_level(line)
        if level is None:
            continue
        candidates.append({"line_index": idx, "text": line, "level": level})

    if not candidates:
        return []

    candidates_by_idx: dict[int, int] = {
        item["line_index"]: item["level"] for item in candidates
    }
    supported = [
        item
        for item in candidates
        if _has_supporting_body(
            item["line_index"],
            lines,
            candidates_by_idx,
            current_level=item["level"],
        )
    ]
    if len(supported) < 2:
        return []

    # Anti-TOC guard: if too many lines are "headings", confidence is low.
    heading_density = len(supported) / max(1, len(non_empty_indexes))
    if heading_density > 0.35:
        return []

    return supported


def _infer_heading_level(line: str) -> int | None:
    """Infer heading level from generic outline markers and line style."""
    if len(line) < 3 or len(line) > 140:
        return None
    if _URL_ONLY_RE.match(line):
        return None
    if line.startswith(("-", "*", "•")):
        return None
    if line[-1] in ".?!;":
        return None

    outline_match = _OUTLINE_PREFIX_RE.match(line)
    if outline_match:
        prefix = outline_match.group("prefix")
        normalized = prefix.rstrip(").-:")
        if re.match(r"^\d+\.\d+", normalized):
            return 3
        return 2

    label_match = _SECTION_LABEL_RE.match(line)
    if label_match:
        label = label_match.group("label").lower()
        return 2 if label in {"chapter", "part", "annex", "appendix", "book", "title"} else 3

    # "Article N ..." is document content, not a structural heading
    if re.match(r"^article\s+\d", line, re.IGNORECASE):
        return None

    words = _WORD_RE.findall(line)
    if not words or len(words) > 12:
        return None

    first_word = words[0].lower()
    if first_word in _GENERIC_HEADING_WORDS:
        return 2

    capitalized = sum(1 for word in words if word[:1].isupper())
    if capitalized / len(words) >= 0.6:
        return 2

    return None


def _has_supporting_body(
    index: int,
    lines: list[str],
    candidates_by_idx: dict[int, int],
    *,
    current_level: int,
    lookahead: int = 12,
) -> bool:
    """Check that a heading candidate is followed by substantive non-heading content.

    Child headings (level > current_level) are transparent — they are skipped
    rather than stopping the search.  Only sibling or parent headings
    (level <= current_level) terminate the lookahead.
    """
    body_signal = 0
    upper = min(len(lines), index + 1 + lookahead)

    for i in range(index + 1, upper):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if i in candidates_by_idx:
            child_level = candidates_by_idx[i]
            if child_level <= current_level:
                # Sibling or parent heading → stop lookahead
                return body_signal >= _MIN_BODY_WORDS_FOR_HEADING
            # Child heading → transparent, keep looking
            continue
        if _URL_ONLY_RE.match(stripped):
            continue
        body_signal += len(_WORD_RE.findall(stripped))
        if body_signal >= _MIN_BODY_WORDS_FOR_HEADING:
            return True

    return body_signal >= _MIN_BODY_WORDS_FOR_HEADING


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
            and merged[-1].heading_path == chunk.heading_path
        ):
            # Merge into previous chunk
            merged[-1].content += "\n\n" + chunk.content
            merged[-1].token_count = _count_tokens(merged[-1].content)
        elif (
            merged
            and merged[-1].token_count < MIN_CHUNK_TOKENS
            and merged[-1].token_count + chunk.token_count <= TARGET_MAX_TOKENS
            and merged[-1].heading_path == chunk.heading_path
        ):
            # Previous chunk is too small; merge current into it
            merged[-1].content += "\n\n" + chunk.content
            merged[-1].token_count = _count_tokens(merged[-1].content)
        else:
            merged.append(chunk)

    return merged


def _split_large_chunk(chunk: _RawChunk) -> list[_RawChunk]:
    """Split a chunk that exceeds TARGET_MAX_TOKENS by paragraph boundaries."""
    paragraphs = _paragraph_candidates(chunk.content)
    result: list[_RawChunk] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_tokens = _count_tokens(para)
        if para_tokens > TARGET_MAX_TOKENS:
            if current_parts:
                result.append(
                    _RawChunk(
                        content="\n\n".join(current_parts),
                        heading_path=list(chunk.heading_path),
                        token_count=current_tokens,
                    )
                )
                current_parts = []
                current_tokens = 0

            result.extend(_split_oversized_text(para, chunk.heading_path))
            continue

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

    return result if result else [chunk]


def _paragraph_candidates(text: str) -> list[str]:
    """Prefer paragraph boundaries, fall back to single newlines for flat web text."""
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if len(paragraphs) > 1:
        return paragraphs

    if "\n" in text:
        lines = [line.strip() for line in re.split(r"\n+", text) if line.strip()]
        if len(lines) > 1:
            return lines

    stripped = text.strip()
    return [stripped] if stripped else []


def _split_oversized_text(text: str, heading_path: list[str]) -> list[_RawChunk]:
    """Split oversized text by sentence boundaries, then by token window."""
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    packed = _pack_segments(sentences)
    if packed:
        return [
            _RawChunk(content=part, heading_path=list(heading_path), token_count=_count_tokens(part))
            for part in packed
        ]

    # Ultimate fallback: hard token-window split, never return > TARGET_MAX_TOKENS.
    encoder = _get_encoder()
    tokens = encoder.encode(text)
    windows: list[_RawChunk] = []
    for i in range(0, len(tokens), TARGET_MAX_TOKENS):
        piece = encoder.decode(tokens[i : i + TARGET_MAX_TOKENS]).strip()
        if not piece:
            continue
        windows.append(
            _RawChunk(
                content=piece,
                heading_path=list(heading_path),
                token_count=_count_tokens(piece),
            )
        )
    return windows


def _pack_segments(segments: list[str]) -> list[str]:
    """Pack segments into <= TARGET_MAX_TOKENS parts; return [] if impossible."""
    if not segments:
        return []

    packed: list[str] = []
    current_text = ""

    for segment in segments:
        seg_tokens = _count_tokens(segment)
        if seg_tokens > TARGET_MAX_TOKENS:
            return []

        candidate = f"{current_text} {segment}".strip() if current_text else segment
        if _count_tokens(candidate) <= TARGET_MAX_TOKENS:
            current_text = candidate
            continue

        if current_text:
            packed.append(current_text)
        current_text = segment

    if current_text:
        packed.append(current_text)

    return [part for part in packed if part.strip()]


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
