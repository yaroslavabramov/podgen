"""LangSmith tracing helpers with safe no-op fallback."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Iterator, Mapping, Sequence

from app.config import settings

logger = logging.getLogger(__name__)

try:
    from langsmith import Client, tracing_context, wrappers
    from langsmith.run_helpers import trace
except Exception:  # pragma: no cover - soft dependency fallback
    Client = None  # type: ignore[assignment]
    tracing_context = None  # type: ignore[assignment]
    wrappers = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]


_trace_context: ContextVar[dict[str, Any]] = ContextVar("podgen_trace_context", default={})
_client_warning_emitted = False
_missing_key_warning_emitted = False


def _has_langsmith_key() -> bool:
    return bool(
        settings.langsmith_api_key.strip()
        or os.getenv("LANGCHAIN_API_KEY", "").strip()
        or os.getenv("LANGSMITH_API_KEY", "").strip()
    )


def tracing_enabled(force: bool | None = None) -> bool:
    global _missing_key_warning_emitted
    if force is not None:
        return force and trace is not None and tracing_context is not None

    enabled = settings.langsmith_tracing and trace is not None and tracing_context is not None
    if not enabled:
        return False

    if not _has_langsmith_key():
        if not _missing_key_warning_emitted:
            logger.warning("LANGSMITH_TRACING is enabled but no API key is configured; tracing disabled.")
            _missing_key_warning_emitted = True
        return False

    return True


@contextmanager
def push_trace_context(values: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    current = dict(_trace_context.get())
    if values:
        current.update({str(k): _to_trace_value(v) for k, v in values.items() if v is not None})
    token = _trace_context.set(current)
    try:
        yield current
    finally:
        _trace_context.reset(token)


def current_trace_context() -> dict[str, Any]:
    return dict(_trace_context.get())


def _parse_base_tags() -> list[str]:
    raw = settings.langsmith_base_tags.strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _merge_tags(tags: Sequence[str] | None = None) -> list[str]:
    merged = _parse_base_tags()
    ctx = current_trace_context()

    phase = ctx.get("phase")
    node = ctx.get("node")
    component = ctx.get("component")
    language = ctx.get("language")

    if isinstance(component, str) and component:
        merged.append(f"component:{component}")
    if isinstance(phase, str) and phase:
        merged.append(f"phase:{phase}")
    if isinstance(node, str) and node:
        merged.append(f"node:{node}")
    if isinstance(language, str) and language:
        merged.append(f"language:{language}")

    if tags:
        merged.extend(str(tag) for tag in tags if str(tag).strip())

    # Deduplicate while preserving order.
    return list(dict.fromkeys(merged))


def _to_trace_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (uuid.UUID, date, datetime)):
        return str(value)
    if isinstance(value, Mapping):
        items = list(value.items())[:100]
        return {str(k): _to_trace_value(v, depth + 1) for k, v in items}
    if isinstance(value, (list, tuple, set)):
        values = list(value)[:200]
        return [_to_trace_value(v, depth + 1) for v in values]
    return str(value)


def _merge_metadata(metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = current_trace_context()
    if metadata:
        merged.update({str(k): _to_trace_value(v) for k, v in metadata.items()})
    return {k: _to_trace_value(v) for k, v in merged.items()}


@lru_cache(maxsize=1)
def _get_langsmith_client() -> Any | None:
    global _client_warning_emitted
    if not tracing_enabled():
        return None
    if Client is None:
        return None

    kwargs: dict[str, Any] = {}
    if settings.langsmith_api_key:
        kwargs["api_key"] = settings.langsmith_api_key
    if settings.langsmith_endpoint:
        kwargs["api_url"] = settings.langsmith_endpoint
    kwargs["tracing_sampling_rate"] = settings.langsmith_sampling_rate

    try:
        return Client(**kwargs)
    except Exception:
        if not _client_warning_emitted:
            logger.warning("LangSmith client init failed, tracing disabled", exc_info=True)
            _client_warning_emitted = True
        return None


@contextmanager
def tracing_scope(
    *,
    metadata: Mapping[str, Any] | None = None,
    tags: Sequence[str] | None = None,
    project_name: str | None = None,
    force: bool | None = None,
) -> Iterator[None]:
    if not tracing_enabled(force):
        yield
        return

    if tracing_context is None:
        yield
        return

    kwargs: dict[str, Any] = {"enabled": True}
    chosen_project = project_name or settings.langsmith_project.strip()
    if chosen_project:
        kwargs["project_name"] = chosen_project

    merged_tags = _merge_tags(tags)
    if merged_tags:
        kwargs["tags"] = merged_tags

    merged_metadata = _merge_metadata(metadata)
    if merged_metadata:
        kwargs["metadata"] = merged_metadata

    client = _get_langsmith_client()
    if client is not None:
        kwargs["client"] = client

    with tracing_context(**kwargs):
        yield


@contextmanager
def trace_span(
    name: str,
    *,
    run_type: str = "chain",
    inputs: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    tags: Sequence[str] | None = None,
    project_name: str | None = None,
    force: bool | None = None,
) -> Iterator[Any | None]:
    if not tracing_enabled(force):
        yield None
        return

    if trace is None:
        yield None
        return

    kwargs: dict[str, Any] = {
        "name": name,
        "run_type": run_type,
    }

    safe_inputs = _to_trace_value(inputs) if inputs is not None else None
    if isinstance(safe_inputs, dict):
        kwargs["inputs"] = safe_inputs

    merged_metadata = _merge_metadata(metadata)
    if merged_metadata:
        kwargs["metadata"] = merged_metadata

    merged_tags = _merge_tags(tags)
    if merged_tags:
        kwargs["tags"] = merged_tags

    chosen_project = project_name or settings.langsmith_project.strip()
    if chosen_project:
        kwargs["project_name"] = chosen_project

    client = _get_langsmith_client()
    if client is not None:
        kwargs["client"] = client

    with trace(**kwargs) as run_tree:
        yield run_tree


def _tracing_extra() -> dict[str, Any] | None:
    if not tracing_enabled():
        return None
    client = _get_langsmith_client()
    if client is None:
        return None
    return {"client": client}


def safe_set_run(
    run_tree: Any | None,
    *,
    outputs: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if run_tree is None:
        return

    kwargs: dict[str, Any] = {}
    if outputs is not None:
        kwargs["outputs"] = _to_trace_value(outputs)
    if metadata is not None:
        kwargs["metadata"] = _to_trace_value(metadata)

    if not kwargs:
        return

    try:
        run_tree.set(**kwargs)
    except Exception:
        logger.debug("LangSmith run_tree.set failed; ignoring tracing update", exc_info=True)


def wrap_openai_client(client: Any) -> Any:
    if wrappers is None or not tracing_enabled():
        return client
    if getattr(client, "_podgen_langsmith_wrapped", False):
        return client

    extra = _tracing_extra()
    wrapped = wrappers.wrap_openai(client, tracing_extra=extra)
    setattr(wrapped, "_podgen_langsmith_wrapped", True)
    return wrapped


def wrap_anthropic_client(client: Any) -> Any:
    if wrappers is None or not tracing_enabled():
        return client
    if getattr(client, "_podgen_langsmith_wrapped", False):
        return client

    extra = _tracing_extra()
    wrapped = wrappers.wrap_anthropic(client, tracing_extra=extra)
    setattr(wrapped, "_podgen_langsmith_wrapped", True)
    return wrapped
