import logging
from typing import Any

import anthropic
import openai

from app.config import settings
from app.observability import safe_set_run, trace_span, wrap_anthropic_client, wrap_openai_client
from app.pipeline.step_control import assert_step_not_stopped

logger = logging.getLogger(__name__)

# Singleton clients
_anthropic_client: anthropic.AsyncAnthropic | None = None
_openai_client: openai.AsyncOpenAI | None = None


def get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        _anthropic_client = wrap_anthropic_client(client)
    return _anthropic_client


def get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        _openai_client = wrap_openai_client(client)
    return _openai_client


SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# Default max tokens for structured (JSON) calls — large enough
# for research trees, episode plans, etc.
DEFAULT_STRUCTURED_MAX_TOKENS = 16384


def _llm_trace_inputs(system: str, prompt: str, max_tokens: int, temperature: float) -> dict[str, Any]:
    return {
        "system_chars": len(system),
        "prompt_chars": len(prompt),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _anthropic_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}

    out: dict[str, Any] = {}
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = getattr(usage, field, None)
        if value is not None:
            out[field] = value
    return out


def _openai_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}

    out: dict[str, Any] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, field, None)
        if value is not None:
            out[field] = value
    return out


async def call_sonnet(system: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.7) -> str:
    client = get_anthropic_client()
    await assert_step_not_stopped()
    with trace_span(
        "llm.call_sonnet",
        run_type="llm",
        inputs=_llm_trace_inputs(system, prompt, max_tokens, temperature),
        metadata={"provider": "anthropic", "model": SONNET_MODEL, "structured": False},
        tags=["llm", "anthropic", "model:sonnet"],
    ) as run_tree:
        response = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        text = response.content[0].text
        safe_set_run(run_tree, outputs={"response_chars": len(text), **_anthropic_usage(response)})
        return text


async def call_haiku(system: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    client = get_anthropic_client()
    await assert_step_not_stopped()
    with trace_span(
        "llm.call_haiku",
        run_type="llm",
        inputs=_llm_trace_inputs(system, prompt, max_tokens, temperature),
        metadata={"provider": "anthropic", "model": HAIKU_MODEL, "structured": False},
        tags=["llm", "anthropic", "model:haiku"],
    ) as run_tree:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        text = response.content[0].text
        safe_set_run(run_tree, outputs={"response_chars": len(text), **_anthropic_usage(response)})
        return text


async def call_sonnet_structured(
    system: str,
    prompt: str,
    max_tokens: int = DEFAULT_STRUCTURED_MAX_TOKENS,
    temperature: float = 0.5,
) -> str:
    """Call Sonnet expecting JSON output."""
    client = get_anthropic_client()
    await assert_step_not_stopped()
    with trace_span(
        "llm.call_sonnet_structured",
        run_type="llm",
        inputs=_llm_trace_inputs(system, prompt, max_tokens, temperature),
        metadata={"provider": "anthropic", "model": SONNET_MODEL, "structured": True},
        tags=["llm", "anthropic", "model:sonnet", "structured"],
    ) as run_tree:
        response = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=max_tokens,
            system=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        if response.stop_reason == "max_tokens":
            logger.warning("Sonnet structured response truncated at %d tokens", max_tokens)
        text = response.content[0].text
        safe_set_run(
            run_tree,
            outputs={
                "response_chars": len(text),
                "stop_reason": response.stop_reason,
                **_anthropic_usage(response),
            },
        )
        return text


async def call_haiku_structured(
    system: str,
    prompt: str,
    max_tokens: int = DEFAULT_STRUCTURED_MAX_TOKENS,
    temperature: float = 0.3,
) -> str:
    """Call Haiku expecting JSON output."""
    client = get_anthropic_client()
    await assert_step_not_stopped()
    with trace_span(
        "llm.call_haiku_structured",
        run_type="llm",
        inputs=_llm_trace_inputs(system, prompt, max_tokens, temperature),
        metadata={"provider": "anthropic", "model": HAIKU_MODEL, "structured": True},
        tags=["llm", "anthropic", "model:haiku", "structured"],
    ) as run_tree:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=max_tokens,
            system=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        if response.stop_reason == "max_tokens":
            logger.warning("Haiku structured response truncated at %d tokens", max_tokens)
        text = response.content[0].text
        safe_set_run(
            run_tree,
            outputs={
                "response_chars": len(text),
                "stop_reason": response.stop_reason,
                **_anthropic_usage(response),
            },
        )
        return text


async def get_embeddings(texts: list[str]) -> list[list[float]]:
    client = get_openai_client()
    await assert_step_not_stopped()
    with trace_span(
        "llm.get_embeddings",
        run_type="embedding",
        inputs={"texts_count": len(texts), "total_chars": sum(len(text) for text in texts)},
        metadata={
            "provider": "openai",
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMENSIONS,
        },
        tags=["embedding", "openai", "model:text-embedding-3-large"],
    ) as run_tree:
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=texts,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        embeddings = [item.embedding for item in response.data]
        safe_set_run(
            run_tree,
            outputs={
                "vectors_count": len(embeddings),
                "vector_dimensions": len(embeddings[0]) if embeddings else 0,
                **_openai_usage(response),
            },
        )
        return embeddings
