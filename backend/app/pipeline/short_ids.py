"""Bidirectional short-ID ↔ UUID mapping for LLM prompts.

LLMs waste tokens and make typos reproducing 36-char UUIDs.
This module maps UUIDs to compact labels (c1, c2, …) before
an LLM call and restores them after parsing.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)


class ShortIdMap:
    """Bidirectional UUID ↔ short-label map.

    Usage::

        sid = ShortIdMap(uuid_list, prefix="c")
        # Before LLM call: replace UUIDs in payload
        short = sid.shorten("5c68d74f-...")   # → "c1"
        # After LLM response: restore UUIDs
        real = sid.restore("c1")              # → "5c68d74f-..."
    """

    def __init__(self, ids: list[str] | set[str], *, prefix: str = "c") -> None:
        self._to_short: dict[str, str] = {}
        self._to_long: dict[str, str] = {}
        for i, long_id in enumerate(sorted(ids), start=1):
            short = f"{prefix}{i}"
            self._to_short[long_id] = short
            self._to_long[short] = long_id

    def shorten(self, long_id: str) -> str:
        """Map UUID → short label.  Returns original if unknown."""
        return self._to_short.get(long_id, long_id)

    def restore(self, short_id: str) -> str:
        """Map short label → UUID.  Returns original if unknown."""
        return self._to_long.get(short_id, short_id)

    def shorten_list(self, ids: list[str]) -> list[str]:
        return [self.shorten(i) for i in ids]

    def restore_list(self, ids: list[str]) -> list[str]:
        return [self.restore(i) for i in ids]

    def is_valid_short(self, short_id: str) -> bool:
        return short_id in self._to_long

    @property
    def short_ids(self) -> set[str]:
        return set(self._to_long.keys())

    @property
    def long_ids(self) -> set[str]:
        return set(self._to_short.keys())

    def __len__(self) -> int:
        return len(self._to_short)

    def __contains__(self, key: str) -> bool:
        return key in self._to_short or key in self._to_long


def shorten_claim_dicts(
    claims: list[dict], sid: ShortIdMap, *, id_field: str = "id"
) -> list[dict]:
    """Return copies of claim dicts with shortened ``id_field``."""
    out: list[dict] = []
    for c in claims:
        copy = dict(c)
        if id_field in copy:
            copy[id_field] = sid.shorten(str(copy[id_field]))
        out.append(copy)
    return out


def restore_id_list(
    raw: Any, sid: ShortIdMap, *, drop_unknown: bool = True
) -> list[str]:
    """Restore a list of short IDs back to UUIDs, optionally dropping unknowns."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        restored = sid.restore(item.strip())
        if restored == item.strip() and drop_unknown and not sid.is_valid_short(item.strip()):
            # Not a known short ID and not a known long ID — skip
            if item.strip() not in sid.long_ids:
                logger.warning("Dropped unknown ID from LLM output: %r", item)
                continue
        out.append(restored)
    return out


def shorten_mapping_keys(mapping: dict[str, Any], sid: ShortIdMap) -> dict[str, Any]:
    """Return a shallow copy of a dict with shortened string keys."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(key, str):
            out[sid.shorten(key)] = value
        else:
            out[key] = value
    return out


def shorten_dialogue_claim_ids(
    dialogue: Any,
    sid: ShortIdMap,
    *,
    field: str = "claim_ids",
) -> Any:
    """Return dialogue-like payload with shortened claim-id lists."""
    if not isinstance(dialogue, list):
        return dialogue
    out: list[Any] = []
    for item in dialogue:
        if not isinstance(item, dict):
            out.append(item)
            continue
        copy = deepcopy(item)
        if isinstance(copy.get(field), list):
            copy[field] = sid.shorten_list(
                [value for value in copy[field] if isinstance(value, str)]
            )
        out.append(copy)
    return out


def restore_dialogue_claim_ids(
    dialogue: Any,
    sid: ShortIdMap,
    *,
    field: str = "claim_ids",
    drop_unknown: bool = True,
) -> Any:
    """Return dialogue-like payload with restored claim-id lists."""
    if not isinstance(dialogue, list):
        return dialogue
    out: list[Any] = []
    for item in dialogue:
        if not isinstance(item, dict):
            out.append(item)
            continue
        copy = deepcopy(item)
        copy[field] = restore_id_list(copy.get(field), sid, drop_unknown=drop_unknown)
        out.append(copy)
    return out
