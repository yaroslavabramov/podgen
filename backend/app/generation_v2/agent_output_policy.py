from __future__ import annotations

from app.config import settings


def semantic_agent_output_limit() -> int | None:
    """Return the optional per-response cap for semantic V2 agents.

    Reasoning tokens and visible/tool output share ``max_output_tokens``. A
    small stage-specific cap can therefore consume the whole response before
    the model reaches a persistence tool. The default is intentionally the
    provider/model limit; the runtime continues an incomplete response in the
    same chain within the configured global provider-round safety budget.
    Operators may still set one global per-response cap for cost control,
    without changing semantic contracts per stage.
    """

    configured = settings.generation_v2_semantic_agent_max_output_tokens
    if configured is None:
        return None
    value = int(configured)
    return value if value > 0 else None


__all__ = ["semantic_agent_output_limit"]
