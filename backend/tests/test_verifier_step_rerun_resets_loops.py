from types import SimpleNamespace
import uuid

import pytest

from app.config import settings
from app.models.claim import VerificationLevel
from app.pipeline.graph import _should_loop_verify
from app.pipeline.phase4_claims.verifier import verify_claims


def test_should_loop_verify_allows_final_loop_with_preincremented_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_verifier_research_loops", 2)

    assert (
        _should_loop_verify(
            {
                "iteration_counts": {
                    "verify_research_loops": 2,
                    "verify_needs_research": True,
                }
            }
        )
        == "research_fast_web"
    )
    assert (
        _should_loop_verify(
            {
                "iteration_counts": {
                    "verify_research_loops": 3,
                    "verify_needs_research": True,
                }
            }
        )
        == "resolve_conflicts"
    )


@pytest.mark.asyncio
async def test_verify_claims_step_rerun_resets_verify_research_loops_and_bypasses_fingerprint_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_uuid = uuid.UUID("00000000-0000-0000-0000-000000000002")
    claim = SimpleNamespace(
        id=claim_uuid,
        statement="A test claim.",
        claim_type="fact",
        attribution=None,
        verification_fingerprint="fp",
        verification_level=VerificationLevel.VERIFIED,
    )

    evidence_data = {
        "has_counter": False,
        "supporting": [],
        "branch_ids": [],
        "question_ids": [],
        "intents": [],
        "direct_evidence_count": 0,
        "contextual_evidence_count": 0,
        "source_ids": set(),
    }

    class _FakeResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        def __init__(self):
            self.commit_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *args, **kwargs):
            return _FakeResult(claim)

        async def commit(self) -> None:
            self.commit_calls += 1

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.async_session_factory",
        lambda: _FakeSession(),
    )
    monkeypatch.setattr("app.pipeline.phase4_claims.verifier.publish_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.update_generation_progress",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier._compute_verification_fingerprint",
        lambda *args, **kwargs: "fp",
    )

    async def _fake_load_claim_evidence(*args, **kwargs):
        return dict(evidence_data)

    monkeypatch.setattr("app.pipeline.phase4_claims.verifier._load_claim_evidence", _fake_load_claim_evidence)

    state = {
        "generation_run_id": "00000000-0000-0000-0000-000000000001",
        "claim_ids": [str(claim_uuid)],
        "iteration_counts": {"verify_research_loops": 2},
        "research_gaps": [{"claim_id": str(claim_uuid)}],
        "manifest_id": "",
        "rebuilt_claim_ids": [],
        "dedupe_merged_into_claim_ids": [],
    }

    async def _fake_force_full_verify_no(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier._should_force_full_verify_for_node_rerun",
        _fake_force_full_verify_no,
    )
    result_no_rerun = await verify_claims(state)

    assert claim.verification_level == VerificationLevel.VERIFIED
    assert result_no_rerun["iteration_counts"]["verify_research_loops"] == 2
    assert result_no_rerun["iteration_counts"]["verify_needs_research"] is False

    async def _fake_force_full_verify_yes(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier._should_force_full_verify_for_node_rerun",
        _fake_force_full_verify_yes,
    )
    result_rerun = await verify_claims(state)

    assert claim.verification_level == VerificationLevel.UNVERIFIED
    assert result_rerun["iteration_counts"]["verify_research_loops"] == 1
    assert result_rerun["iteration_counts"]["verify_needs_research"] is True

