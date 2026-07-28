from __future__ import annotations

from typing import Any


_EMPTY_OPTIONAL_STRINGS = {"", "null", "none", "nil", "undefined"}


def optional_model_text(value: Any) -> str:
    """Normalize model-filled optional string fields.

    Tool schemas often require unused selector fields to be present as empty
    strings. Models sometimes send JSON-ish sentinels such as "null" instead.
    Treat those as absent so the agent is not penalized for a transport detail.
    """

    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold() in _EMPTY_OPTIONAL_STRINGS:
        return ""
    return text


def has_optional_model_value(value: Any) -> bool:
    return bool(optional_model_text(value))


__all__ = ["has_optional_model_value", "optional_model_text"]
