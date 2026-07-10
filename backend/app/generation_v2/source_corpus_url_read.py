from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.generation_v2.model_routing import generation_v2_corpus_prepare_model
from app.generation_v2.source_corpus_contracts import OpenAIUrlRecoveryStatus
from app.generation_v2.source_corpus_url_schemas import OPENAI_URL_READ_SCHEMA, OPENAI_URL_RECOVERY_SCHEMA
from app.services.llm import _openai_response_model, get_openai_client

async def call_openai_url_file_read(url: str, *, client: Any | None = None) -> dict[str, Any]:
    client = client or get_openai_client()
    requested_model = generation_v2_corpus_prepare_model()
    prompt = (
        "Read the attached external URL/file as a source for a podcast research corpus. "
        "Use only the attached file_url content. Do not rely on memory or external knowledge. "
        "If the file cannot be read, is blocked, is gone, or is unsupported, return that status and leave "
        "extracted_text_markdown empty. If readable, extract faithful article/document text in Markdown, "
        "preserving headings and useful locator cues. Do not invent missing text."
        f"\n\nURL: {url}"
    )
    response = await client.responses.create(
        model=requested_model,
        instructions="You are a careful source-ingestion reader. Return JSON only.",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_file", "file_url": url},
                ],
            }
        ],
        max_output_tokens=settings.generation_v2_openai_url_read_max_output_tokens,
        text={
            "format": {
                "type": "json_schema",
                "name": "OpenAIUrlReadResult",
                "schema": OPENAI_URL_READ_SCHEMA,
                "strict": True,
            }
        },
    )
    payload = loads_json_object(getattr(response, "output_text", "") or "")
    payload["_response_id"] = getattr(response, "id", "")
    payload["_requested_model"] = requested_model
    payload["_response_model"] = _openai_response_model(response)
    return payload


async def call_openai_url_recovery(
    url: str,
    read_metadata: dict[str, Any],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    client = client or get_openai_client()
    requested_model = generation_v2_corpus_prepare_model()
    prompt = (
        "The product tried to ingest this URL as source material, but direct URL/file reading did not produce "
        "usable source text. Use web search only to diagnose whether the URL is blocked/gone/paywalled and to "
        "find official or high-confidence alternate URLs for the same source. Do not summarize the topic broadly. "
        "Return only recovery metadata and candidate URLs; do not pretend a candidate is the same source unless the "
        "title/identifier clearly matches.\n\n"
        f"URL: {url}\n"
        f"Prior read metadata: {json.dumps(read_metadata.get('v2_openai_url_read', read_metadata), ensure_ascii=False)[:3000]}"
    )
    response = await client.responses.create(
        model=requested_model,
        instructions="You are a conservative source recovery agent. Return JSON only.",
        input=prompt,
        tools=[{"type": "web_search"}],
        max_output_tokens=3000,
        text={
            "format": {
                "type": "json_schema",
                "name": "OpenAIUrlRecoveryResult",
                "schema": OPENAI_URL_RECOVERY_SCHEMA,
                "strict": True,
            }
        },
    )
    output_text = getattr(response, "output_text", "") or ""
    if not output_text.strip():
        return {
            "recovery_status": OpenAIUrlRecoveryStatus.UNCLEAR,
            "original_url_status": "unknown",
            "best_alternative_url": "",
            "alternative_urls": [],
            "notes": "OpenAI web_search recovery returned an empty structured response.",
            "_response_id": getattr(response, "id", ""),
            "_requested_model": requested_model,
            "_response_model": _openai_response_model(response),
        }
    payload = loads_json_object(output_text)
    payload["_response_id"] = getattr(response, "id", "")
    payload["_requested_model"] = requested_model
    payload["_response_model"] = _openai_response_model(response)
    return payload


def format_openai_url_read_document(url: str, payload: dict[str, Any], text: str) -> str:
    title = str(payload.get("title") or "Untitled source").strip()
    canonical_url = str(payload.get("canonical_url") or url).strip()
    summary = str(payload.get("summary") or "").strip()
    return (
        f"# {title}\n\n"
        f"Source URL: {url}\n"
        f"Canonical URL: {canonical_url}\n"
        f"OpenAI URL read status: {payload.get('read_status')}\n"
        f"Confidence: {payload.get('confidence')}\n\n"
        f"## OpenAI Read Summary\n\n{summary}\n\n"
        f"## Extracted Text\n\n{text}\n"
    )


def loads_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("OpenAI URL read returned non-object JSON")
    return value
