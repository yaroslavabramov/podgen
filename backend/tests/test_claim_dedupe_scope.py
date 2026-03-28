from datetime import datetime
from types import SimpleNamespace
import uuid

import pytest

from app.models.claim import Claim, ClaimEvidence
from app.pipeline.phase4_claims.dedupe import dedupe_claims, _restore_missing_claims_from_snapshot


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


class _FakeExistingResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


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
    async def _fake_execution_context(db, generation_run_id):
        return {
            "input_origin": "checkpoint_pre_node_state",
            "is_step_rerun": True,
            "requested_action": "rerun",
            "requested_target_node": "dedupe_claims",
            "last_node": "dedupe_claims",
        }

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
        "app.pipeline.phase4_claims.dedupe._load_dedupe_execution_context",
        _fake_execution_context,
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
    async def _fake_execution_context(db, generation_run_id):
        return {
            "input_origin": "checkpoint_pre_node_state",
            "is_step_rerun": True,
            "requested_action": "rerun",
            "requested_target_node": "dedupe_claims",
            "last_node": "dedupe_claims",
        }

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
        "app.pipeline.phase4_claims.dedupe._load_dedupe_execution_context",
        _fake_execution_context,
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
    assert captured_debug["execution_context"]["input_origin"] == "checkpoint_pre_node_state"
    assert captured_debug["execution_context"]["is_step_rerun"] is True


@pytest.mark.asyncio
async def test_dedupe_claims_commits_restored_claims_even_when_no_merges_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    claims = [
        SimpleNamespace(id="c1", created_at=datetime.utcnow(), notes=""),
        SimpleNamespace(id="c-restored", created_at=datetime.utcnow(), notes=""),
    ]
    contexts = {
        "c1": {"marker": "keep"},
        "c-restored": {"marker": "restored"},
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

    async def _fake_execution_context(db, generation_run_id):
        return {
            "input_origin": "checkpoint_pre_node_state",
            "is_step_rerun": True,
            "requested_action": "rerun",
            "requested_target_node": "dedupe_claims",
            "last_node": "dedupe_claims",
        }

    async def _fake_restore_missing_claims_from_snapshot(**_kwargs):
        return ["c-restored"]

    async def _fake_load_claim_context(db, claim_ids, generation_run_id):
        return claims, contexts, {
            "requested_claim_id_count": len(claim_ids),
            "loaded_claim_row_count": len(claims),
            "missing_claim_id_count": 0,
            "missing_claim_ids": [],
        }

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._load_dedupe_execution_context",
        _fake_execution_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._restore_missing_claims_from_snapshot",
        _fake_restore_missing_claims_from_snapshot,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._load_claim_context",
        _fake_load_claim_context,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._claims_match",
        lambda *_args, **_kwargs: False,
    )

    result = await dedupe_claims(
        {
            "generation_run_id": "00000000-0000-0000-0000-000000000001",
            "project_id": "00000000-0000-0000-0000-000000000002",
            "claim_ids": ["c1", "c-restored"],
            "dedupe_source_claims": [{"claim_id": "c-restored"}],
            "rebuilt_claim_ids": [],
        }
    )

    assert result["claim_ids"] == ["c1", "c-restored"]
    assert session.commit_calls == 1
    assert session.deleted_ids == []


@pytest.mark.asyncio
async def test_restore_missing_claims_from_snapshot_recreates_missing_rows() -> None:
    claim_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())

    class _FakeDB:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def execute(self, *_args, **_kwargs):
            return _FakeExistingResult([])

        def add(self, item: object) -> None:
            self.added.append(item)

        async def flush(self) -> None:
            return None

    db = _FakeDB()
    restored = await _restore_missing_claims_from_snapshot(
        db=db,
        project_id=project_id,
        requested_claim_ids=[claim_id],
        snapshot_claims=[
            {
                "claim_id": claim_id,
                "claim_type": "fact",
                "verification_level": "observed",
                "verification_fingerprint": "fp-1",
                "statement": "Recovered claim",
                "attribution": {"speaker": "source"},
                "as_of_date": None,
                "notes": "snapshot notes",
                "created_at": "2026-03-29T10:11:12",
                "updated_at": "2026-03-29T10:11:13",
                "evidence_links": [
                    {
                        "evidence_id": evidence_id,
                        "supports_fact": True,
                        "contradicts_fact": False,
                        "illustrative": True,
                    }
                ],
            }
        ],
    )

    created_claims = [item for item in db.added if isinstance(item, Claim)]
    created_links = [item for item in db.added if isinstance(item, ClaimEvidence)]

    assert restored == [claim_id]
    assert len(created_claims) == 1
    assert len(created_links) == 1
    assert str(created_claims[0].id) == claim_id
    assert created_claims[0].statement == "Recovered claim"
    assert created_claims[0].verification_fingerprint == "fp-1"
    assert str(created_links[0].evidence_id) == evidence_id
    assert created_links[0].supports_fact is True
    assert created_links[0].illustrative is True
