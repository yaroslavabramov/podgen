import logging

import anthropic
import openai

from app.config import settings

logger = logging.getLogger(__name__)

# Singleton clients
_anthropic_client: anthropic.AsyncAnthropic | None = None
_openai_client: openai.AsyncOpenAI | None = None


def get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# Default max tokens for structured (JSON) calls — large enough
# for research trees, episode plans, etc.
DEFAULT_STRUCTURED_MAX_TOKENS = 16384


async def call_sonnet(system: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.7) -> str:
    client = get_anthropic_client()
    response = await client.messages.create(
        model=SONNET_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return response.content[0].text


async def call_haiku(system: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    client = get_anthropic_client()
    response = await client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return response.content[0].text


async def call_sonnet_structured(system: str, prompt: str, max_tokens: int = DEFAULT_STRUCTURED_MAX_TOKENS, temperature: float = 0.5) -> str:
    """Call Sonnet expecting JSON output."""
    client = get_anthropic_client()
    response = await client.messages.create(
        model=SONNET_MODEL,
        max_tokens=max_tokens,
        system=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    if response.stop_reason == "max_tokens":
        logger.warning("Sonnet structured response truncated at %d tokens", max_tokens)
    return response.content[0].text


async def call_haiku_structured(system: str, prompt: str, max_tokens: int = DEFAULT_STRUCTURED_MAX_TOKENS, temperature: float = 0.3) -> str:
    """Call Haiku expecting JSON output."""
    client = get_anthropic_client()
    response = await client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=max_tokens,
        system=system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation.",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    if response.stop_reason == "max_tokens":
        logger.warning("Haiku structured response truncated at %d tokens", max_tokens)
    return response.content[0].text


async def get_embeddings(texts: list[str]) -> list[list[float]]:
    client = get_openai_client()
    response = await client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    return [item.embedding for item in response.data]
