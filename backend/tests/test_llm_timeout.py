import asyncio

import pytest

from app.config import settings
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
