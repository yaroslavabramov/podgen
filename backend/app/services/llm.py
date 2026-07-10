import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import anthropic
import httpx
import openai

from app.config import settings
from app.observability import safe_set_run, trace_span, wrap_anthropic_client, wrap_openai_client
from app.pipeline.step_control import assert_step_not_stopped

logger = logging.getLogger(__name__)

_OVERLOAD_RETRY_DELAYS = (30, 60)  # seconds between retries on 529


def _request_timeout_seconds() -> int:
    return max(1, int(settings.llm_request_timeout_seconds))


def _openai_connect_timeout_seconds() -> int:
    return max(1, int(settings.openai_connect_timeout_seconds))


def _openai_max_retries() -> int:
    return max(0, int(settings.openai_max_retries))


async def _call_with_overload_retry(
    fn: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    """Retry an Anthropic API call on 529 overloaded with escalating delays."""
    for attempt, delay in enumerate((*_OVERLOAD_RETRY_DELAYS, None)):
        try:
            return await fn()
        except anthropic.APIStatusError as e:
            if e.status_code != 529 or delay is None:
                raise
            logger.warning(
                "Anthropic overloaded (529), retrying in %ds (attempt %d/%d)",
                delay,
                attempt + 1,
                len(_OVERLOAD_RETRY_DELAYS) + 1,
            )
            await assert_step_not_stopped()
            await asyncio.sleep(delay)
            await assert_step_not_stopped()


async def _call_with_request_timeout(
    label: str,
    fn: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    timeout_seconds = _request_timeout_seconds()
    try:
        return await asyncio.wait_for(_call_with_overload_retry(fn), timeout=timeout_seconds)
    except TimeoutError as exc:
        logger.error("%s timed out after %ss", label, timeout_seconds)
        raise TimeoutError(f"{label} timed out after {timeout_seconds}s") from exc


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
        client = openai.AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=httpx.Timeout(
                float(_request_timeout_seconds()),
                connect=float(_openai_connect_timeout_seconds()),
            ),
            max_retries=_openai_max_retries(),
        )
        _openai_client = wrap_openai_client(client)
    return _openai_client


SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# Default max tokens for structured (JSON) calls — large enough
# for research trees, episode plans, etc.
DEFAULT_STRUCTURED_MAX_TOKENS = 16384


OPENAI_TOKEN_COST_BASIS = "openai_token_usage_only"
OPENAI_TOKEN_COST_WARNING = (
    "Token-cost estimate only; this is not an OpenAI invoice total and can exclude hosted-tool, "
    "file/vector storage or search, retry, tax, credit, and adjustment charges."
)


def _openai_response_model(response: Any) -> str | None:
    model = getattr(response, "model", None)
    if model:
        return str(model)
    if hasattr(response, "model_dump"):
        try:
            payload = response.model_dump()
            if payload.get("model"):
                return str(payload["model"])
        except Exception:
            return None
    return None


def _llm_trace_inputs(system: str, prompt: str, max_tokens: int, temperature: float | None) -> dict[str, Any]:
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
    for field in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
        value = getattr(usage, field, None)
        if value is not None:
            out[field] = value
    for detail_field in ("input_tokens_details", "prompt_tokens_details"):
        details = _usage_detail(usage, detail_field)
        cached_tokens = _usage_detail_value(details, "cached_tokens")
        if cached_tokens is not None:
            out["cached_input_tokens"] = cached_tokens
            break
    output_details = _usage_detail(usage, "output_tokens_details") or _usage_detail(usage, "completion_tokens_details")
    reasoning_tokens = _usage_detail_value(output_details, "reasoning_tokens")
    if reasoning_tokens is not None:
        out["reasoning_output_tokens"] = reasoning_tokens
    return out


def _usage_detail(usage: Any, field: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(field)
    if hasattr(usage, "model_dump"):
        try:
            return usage.model_dump().get(field)
        except Exception:
            pass
    return getattr(usage, field, None)


def _usage_detail_value(details: Any, field: str) -> int | None:
    if not details:
        return None
    value = details.get(field) if isinstance(details, dict) else getattr(details, field, None)
    return int(value) if value is not None else None


def _openai_estimated_token_cost_usd(model: str, usage: dict[str, Any]) -> float | None:
    rates = _openai_token_rates(model)
    if rates is None:
        return None
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cached_tokens = min(input_tokens, int(usage.get("cached_input_tokens") or 0))
    uncached_tokens = max(0, input_tokens - cached_tokens)
    cost = (
        uncached_tokens * rates["input_per_1m"]
        + cached_tokens * rates["cached_input_per_1m"]
        + output_tokens * rates["output_per_1m"]
    ) / 1_000_000
    return round(cost, 8)


def _openai_estimated_cost_usd(model: str, usage: dict[str, Any]) -> float | None:
    return _openai_estimated_token_cost_usd(model, usage)


def _openai_token_cost_payload(model: str, usage: dict[str, Any]) -> dict[str, Any]:
    token_cost = _openai_estimated_token_cost_usd(model, usage)
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    cached_tokens = min(input_tokens, int(usage.get("cached_input_tokens") or 0))
    return {
        "estimated_token_cost_usd": token_cost,
        "uncached_input_tokens": max(0, input_tokens - cached_tokens),
        "cost_basis": OPENAI_TOKEN_COST_BASIS,
        "cost_warning": OPENAI_TOKEN_COST_WARNING,
    }


def _openai_trace_usage_payload(
    *,
    requested_model: str,
    response_model: str | None,
    billing_model: str,
    usage: dict[str, Any],
    token_cost_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "requested_model": requested_model,
        "response_model": response_model,
        "billing_model": billing_model,
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
        "cached_input_tokens": usage.get("cached_input_tokens") or 0,
        "uncached_input_tokens": token_cost_payload.get("uncached_input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
        "reasoning_output_tokens": usage.get("reasoning_output_tokens") or 0,
        "estimated_token_cost_usd": token_cost_payload.get("estimated_token_cost_usd"),
        "cost_basis": token_cost_payload.get("cost_basis"),
        "cost_warning": token_cost_payload.get("cost_warning"),
    }


def _openai_token_rates(model: str) -> dict[str, float] | None:
    normalized = (model or "").lower()
    if normalized.startswith("gpt-5.5"):
        return {"input_per_1m": 5.00, "cached_input_per_1m": 0.50, "output_per_1m": 30.00}
    if normalized.startswith("gpt-5.4-mini"):
        return {"input_per_1m": 0.75, "cached_input_per_1m": 0.075, "output_per_1m": 4.50}
    if normalized.startswith("gpt-5.4"):
        return {"input_per_1m": 2.50, "cached_input_per_1m": 0.25, "output_per_1m": 15.00}
    if normalized == "gpt-5":
        return {"input_per_1m": 1.25, "cached_input_per_1m": 0.125, "output_per_1m": 10.0}
    return None


async def call_sonnet(system: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.7) -> str:
    client = get_anthropic_client()
    await assert_step_not_stopped()
    with trace_span(
        "llm.call_sonnet",
        run_type="llm",
        inputs=_llm_trace_inputs(system, prompt, max_tokens, temperature),
        metadata={
            "provider": "anthropic",
            "model": SONNET_MODEL,
            "structured": False,
            "timeout_seconds": _request_timeout_seconds(),
        },
        tags=["llm", "anthropic", "model:sonnet"],
    ) as run_tree:
        response = await _call_with_request_timeout(
            "Anthropic Sonnet request",
            lambda: client.messages.create(
                model=SONNET_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
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
        metadata={
            "provider": "anthropic",
            "model": HAIKU_MODEL,
            "structured": False,
            "timeout_seconds": _request_timeout_seconds(),
        },
        tags=["llm", "anthropic", "model:haiku"],
    ) as run_tree:
        response = await _call_with_request_timeout(
            "Anthropic Haiku request",
            lambda: client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
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
        metadata={
            "provider": "anthropic",
            "model": SONNET_MODEL,
            "structured": True,
            "timeout_seconds": _request_timeout_seconds(),
        },
        tags=["llm", "anthropic", "model:sonnet", "structured"],
    ) as run_tree:
        response = await _call_with_request_timeout(
            "Anthropic Sonnet structured request",
            lambda: client.messages.create(
                model=SONNET_MODEL,
                max_tokens=max_tokens,
                system=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
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
        metadata={
            "provider": "anthropic",
            "model": HAIKU_MODEL,
            "structured": True,
            "timeout_seconds": _request_timeout_seconds(),
        },
        tags=["llm", "anthropic", "model:haiku", "structured"],
    ) as run_tree:
        response = await _call_with_request_timeout(
            "Anthropic Haiku structured request",
            lambda: client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=max_tokens,
                system=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
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


async def call_openai_structured(
    system: str,
    prompt: str,
    *,
    model: str,
    schema: dict[str, Any],
    schema_name: str = "structured_response",
    strict: bool = True,
    max_tokens: int = DEFAULT_STRUCTURED_MAX_TOKENS,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Call OpenAI Responses API expecting JSON output."""
    client = get_openai_client()
    await assert_step_not_stopped()
    reasoning = {"effort": reasoning_effort} if reasoning_effort else openai.NOT_GIVEN
    temperature_arg = temperature if temperature is not None else openai.NOT_GIVEN
    with trace_span(
        "llm.call_openai_structured",
        run_type="llm",
        inputs=_llm_trace_inputs(system, prompt, max_tokens, temperature),
        metadata={
            "provider": "openai",
            "model": model,
            "structured": True,
            "reasoning_effort": reasoning_effort or "",
            "timeout_seconds": _request_timeout_seconds(),
        },
        tags=["llm", "openai", f"model:{model}", "structured"],
    ) as run_tree:
        response = await _call_with_request_timeout(
            "OpenAI structured request",
            lambda: client.responses.create(
                model=model,
                instructions=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
                input=prompt,
                max_output_tokens=max_tokens,
                temperature=temperature_arg,
                reasoning=reasoning,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": strict,
                    }
                },
            ),
        )
        status = getattr(response, "status", "")
        incomplete_details = getattr(response, "incomplete_details", None)
        if status == "incomplete":
            logger.warning("OpenAI structured response incomplete: %s", incomplete_details)
        text = response.output_text or ""
        safe_set_run(
            run_tree,
            outputs={
                "response_chars": len(text),
                "status": status,
                "incomplete_details": str(incomplete_details) if incomplete_details else None,
                **_openai_usage(response),
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
