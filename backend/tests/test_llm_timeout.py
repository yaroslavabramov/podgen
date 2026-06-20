import asyncio

import pytest

from app.config import settings
from app.services import llm
from app.services.llm import _call_with_request_timeout


@pytest.mark.asyncio
async def test_call_with_request_timeout_raises_after_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 1)

    async def _slow_call():
        await asyncio.sleep(2)
        return "late"

    with pytest.raises(TimeoutError, match="timed out after 1s"):
        await _call_with_request_timeout("Test LLM request", _slow_call)


def test_openai_client_uses_configured_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 300)
    monkeypatch.setattr(settings, "openai_connect_timeout_seconds", 60)
    monkeypatch.setattr(settings, "openai_max_retries", 4)
    monkeypatch.setattr(llm, "_openai_client", None)
    monkeypatch.setattr(llm, "wrap_openai_client", lambda client: client)

    client = llm.get_openai_client()

    assert client.timeout.connect == 60.0
    assert client.timeout.read == 300.0
    assert client.timeout.write == 300.0
    assert client.max_retries == 4
