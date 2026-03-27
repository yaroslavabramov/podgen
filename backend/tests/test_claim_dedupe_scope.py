from datetime import datetime
from types import SimpleNamespace

import pytest

from app.pipeline.phase4_claims.dedupe import dedupe_claims


class _FakeSession:
    def __init__(self) -> None:
        self.deleted_ids: list[str] = []
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def delete(self, claim) -> None:
        self.deleted_ids.append(str(claim.id))

    async def commit(self) -> None:
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_dedupe_claims_reports_touched_keep_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    claims = [
        SimpleNamespace(id="c1", created_at=datetime.utcnow(), notes=""),
        SimpleNamespace(id="c2", created_at=datetime.utcnow(), notes=""),
        SimpleNamespace(id="c3", created_at=datetime.utcnow(), notes=""),
    ]
    contexts = {
        "c1": {"marker": "keep"},
        "c2": {"marker": "drop"},
        "c3": {"marker": "rebuilt"},
    }

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe.async_session_factory",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe.update_generation_progress",
        lambda *args, **kwargs: None,
    )

    async def _fake_load_claim_context(db, claim_ids, generation_run_id):
        return claims, contexts, {
            "requested_claim_id_count": len(claim_ids),
            "loaded_claim_row_count": len(claims),
            "missing_claim_id_count": 0,
            "missing_claim_ids": [],
        }

    async def _fake_merge_claims(db, *, keep_claim_id, drop_claim_id):
        return None

    def _fake_claims_match(left, right):
        return left["marker"] == "keep" and right["marker"] == "drop"

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._load_claim_context",
        _fake_load_claim_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._merge_claims",
        _fake_merge_claims,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._claims_match",
        _fake_claims_match,
    )

    result = await dedupe_claims(
        {
            "generation_run_id": "00000000-0000-0000-0000-000000000001",
            "claim_ids": ["c1", "c2", "c3"],
            "rebuilt_claim_ids": ["c3"],
        }
    )

    assert session.deleted_ids == ["c2"]
    assert session.commit_calls == 1
    assert result["claim_ids"] == ["c1", "c3"]
    assert result["dedupe_deleted_claim_ids"] == ["c2"]
    assert result["dedupe_merged_into_claim_ids"] == ["c1"]
    assert result["dedupe_untouched_claim_ids"] == []


@pytest.mark.asyncio
async def test_dedupe_claims_returns_sanitized_live_pool_when_state_contains_missing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    claims = [
        SimpleNamespace(id="c1", created_at=datetime.utcnow(), notes=""),
        SimpleNamespace(id="c2", created_at=datetime.utcnow(), notes=""),
    ]
    contexts = {
        "c1": {"marker": "keep"},
        "c2": {"marker": "keep"},
    }
    captured_debug: dict = {}

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe.async_session_factory",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe.update_generation_progress",
        lambda *args, **kwargs: None,
    )

    async def _fake_load_claim_context(db, claim_ids, generation_run_id):
        return claims, contexts, {
            "requested_claim_id_count": len(claim_ids),
            "loaded_claim_row_count": len(claims),
            "missing_claim_id_count": 1,
            "missing_claim_ids": ["c-missing"],
        }

    async def _fake_merge_claims(db, *, keep_claim_id, drop_claim_id):
        return None

    def _fake_claims_match(left, right):
        return False

    def _fake_dump(**kwargs):
        captured_debug.update(kwargs)

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._load_claim_context",
        _fake_load_claim_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._merge_claims",
        _fake_merge_claims,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._claims_match",
        _fake_claims_match,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._dump_dedupe_debug",
        _fake_dump,
    )

    result = await dedupe_claims(
        {
            "generation_run_id": "00000000-0000-0000-0000-000000000001",
            "claim_ids": ["c1", "c2", "c-missing"],
            "rebuilt_claim_ids": [],
        }
    )

    assert result["claim_ids"] == ["c1", "c2"]
    assert captured_debug["requested_claim_id_count"] == 3
    assert captured_debug["loaded_claim_row_count"] == 2
    assert captured_debug["missing_claim_id_count"] == 1
    assert captured_debug["missing_claim_ids"] == ["c-missing"]
