import uuid

import pytest

from app.ingestion.router import WebQualityIngestionError, run_ingestion
from app.models.source import Source, SourceType


class _DummySession:
    async def flush(self) -> None:
        return None

    async def execute(self, *args, **kwargs):  # pragma: no cover - should not be called for WEB
        raise AssertionError("execute() is not expected in this test")


@pytest.mark.asyncio
async def test_blocked_web_source_fails_instead_of_silent_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Source(
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/protected",
    )
    db = _DummySession()

    async def _fake_extract_web(url: str) -> tuple[str, str | None, str | None, str]:
        return (
            "Warning: Target URL returned error 403: Forbidden\n"
            "Just a moment...\n"
            "Performing security verification\n",
            None,
            None,
            "jina",
        )

    monkeypatch.setattr("app.ingestion.router.extract_web_with_profile", _fake_extract_web)
    monkeypatch.setattr("app.ingestion.router.get_minio_client", lambda: object())
    monkeypatch.setattr("app.ingestion.router.upload_text", lambda *args, **kwargs: None)

    async def _should_not_chunk(*args, **kwargs):
        raise AssertionError("chunking must not run for blocked web sources")

    monkeypatch.setattr("app.ingestion.router.structural_chunk", _should_not_chunk)

    with pytest.raises(WebQualityIngestionError, match="blocked by anti-bot/security wall") as exc_info:
        await run_ingestion(source, db)

    assert exc_info.value.invalidate_native_ingest is True
    assert exc_info.value.metadata_patch()["web_quality"]["blocked"] is True
    assert isinstance(source.metadata_, dict)
    assert source.metadata_["web_quality"]["blocked"] is True


@pytest.mark.asyncio
async def test_low_signal_web_source_fails_instead_of_creating_tiny_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Source(
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/js-shell",
    )
    db = _DummySession()

    async def _fake_extract_web(url: str) -> tuple[str, str | None, str | None, str]:
        return (
            "Title:\n\nURL Source: https://example.com/js-shell\n\nMarkdown Content:",
            "Title:",
            None,
            "jina",
        )

    monkeypatch.setattr("app.ingestion.router.extract_web_with_profile", _fake_extract_web)
    monkeypatch.setattr("app.ingestion.router.get_minio_client", lambda: object())
    monkeypatch.setattr("app.ingestion.router.upload_text", lambda *args, **kwargs: None)

    async def _should_not_chunk(*args, **kwargs):
        raise AssertionError("chunking must not run for low-signal web sources")

    monkeypatch.setattr("app.ingestion.router.structural_chunk", _should_not_chunk)

    with pytest.raises(WebQualityIngestionError, match="low-signal text") as exc_info:
        await run_ingestion(source, db)

    assert exc_info.value.invalidate_native_ingest is True
    assert exc_info.value.metadata_patch()["web_quality"]["state"] == "low_signal"
    assert isinstance(source.metadata_, dict)
    assert source.metadata_["web_quality"]["state"] == "low_signal"
    assert source.metadata_["web_quality"]["blocked"] is False
