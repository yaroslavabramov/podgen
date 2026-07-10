from __future__ import annotations

from app.generation_v2.schemas import QuoteCard
from app.models.chunk import Chunk
from app.models.generation_v2 import SourceRepresentationKind
from app.models.source import Source


def compact_preview(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "No extracted preview is available; inspect the original source representation."
    return cleaned[:limit]


def infer_useful_for(text: str) -> list[str]:
    lower = (text or "").lower()
    buckets: list[str] = []
    for label, markers in {
        "context": ("history", "timeline", "founded", "began", "started", "background"),
        "mechanism": ("mechanism", "because", "how it works", "process", "system"),
        "tension": ("conflict", "debate", "critic", "lawsuit", "controvers", "tradeoff"),
        "impact": ("impact", "result", "consequence", "cost", "effect"),
    }.items():
        if any(marker in lower for marker in markers):
            buckets.append(label)
    return buckets or ["background"]


def infer_cautions(source: Source, text: str) -> list[str]:
    cautions: list[str] = []
    if not text.strip():
        cautions.append("No deterministic text view is available; rely on original/provider representation.")
    if source.domain_reliability in {"low", "unknown"}:
        cautions.append(f"Reliability is {source.domain_reliability}.")
    if source.source_type == "web" and not source.published_at:
        cautions.append("Published date is missing.")
    return cautions


def quote_cards_from_chunks(source: Source, chunks: list[Chunk], *, representation_id: str | None = None) -> list[QuoteCard]:
    cards: list[QuoteCard] = []
    for chunk in chunks[:5]:
        quote = best_quote(chunk.content)
        if not quote:
            continue
        cards.append(
            QuoteCard(
                source_id=str(source.id),
                quote=quote,
                locator=" > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}",
                narrative_use=", ".join(infer_useful_for(chunk.content)),
                representation_id=representation_id,
                source_kind=SourceRepresentationKind.EXTRACTED_TEXT,
                confidence=0.65,
                confidence_reason="Selected from deterministic chunk text.",
            )
        )
    return cards


def best_quote(text: str) -> str:
    paragraphs = [p.strip() for p in (text or "").split("\n") if len(p.strip()) >= 80]
    if not paragraphs:
        return ""
    return compact_preview(paragraphs[0], 420)
