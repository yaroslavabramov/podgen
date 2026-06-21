from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.generation_v2.schemas import SourceMap


@dataclass(frozen=True)
class SourceRefContext:
    inventory: SourceMap
    source_id_by_ref: dict[str, str]
    ref_by_source_id: dict[str, str]
    card_by_source_id: dict[str, Any]


def build_source_ref_context(inventory: SourceMap) -> SourceRefContext:
    cards = list(inventory.source_cards or [])
    width = max(2, len(str(max(1, len(cards)))))
    source_id_by_ref: dict[str, str] = {}
    ref_by_source_id: dict[str, str] = {}
    card_by_source_id: dict[str, Any] = {}
    for idx, card in enumerate(cards, start=1):
        source_id = str(card.source_id)
        source_ref = f"S{idx:0{width}d}"
        source_id_by_ref[source_ref] = source_id
        ref_by_source_id[source_id] = source_ref
        card_by_source_id[source_id] = card
    return SourceRefContext(
        inventory=inventory,
        source_id_by_ref=source_id_by_ref,
        ref_by_source_id=ref_by_source_id,
        card_by_source_id=card_by_source_id,
    )


def build_source_ref_context_from_ids(source_ids: list[str]) -> SourceRefContext:
    cards = [
        SimpleNamespace(
            source_id=str(source_id),
            source_version_ids=[],
            title="Unknown source",
            source_type="unknown",
            domain="",
            published_at=None,
            authority="unknown",
            reliability="unknown",
            coverage_status="partial",
            representation_summary="",
            summary="",
        )
        for source_id in source_ids
    ]
    return build_source_ref_context(SimpleNamespace(source_cards=cards))


def resolve_source_ref(value: str, context: SourceRefContext) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in context.source_id_by_ref:
        return context.source_id_by_ref[text]
    if text in context.ref_by_source_id:
        return text
    return None


def source_ref_for_id(source_id: str, context: SourceRefContext) -> str:
    return context.ref_by_source_id.get(str(source_id), "S??")


def source_card_for_id(source_id: str, context: SourceRefContext) -> Any | None:
    return context.card_by_source_id.get(str(source_id))


def source_title_for_id(source_id: str, context: SourceRefContext) -> str:
    card = source_card_for_id(source_id, context)
    if card is None:
        return "Unknown source"
    return str(getattr(card, "title", "") or "Untitled source")


def source_domain_for_id(source_id: str, context: SourceRefContext) -> str:
    card = source_card_for_id(source_id, context)
    if card is None:
        return ""
    return str(getattr(card, "domain", "") or "")


def version_ref_for_index(source_ref: str, *, index: int, canonical: bool) -> str:
    if canonical:
        return f"{source_ref}:canonical"
    return f"{source_ref}:v{index}"


__all__ = [
    "SourceRefContext",
    "build_source_ref_context",
    "build_source_ref_context_from_ids",
    "resolve_source_ref",
    "source_card_for_id",
    "source_domain_for_id",
    "source_ref_for_id",
    "source_title_for_id",
    "version_ref_for_index",
]
