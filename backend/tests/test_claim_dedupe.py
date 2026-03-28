import json
import uuid
from types import SimpleNamespace

import pytest

from app.pipeline.phase4_claims.dedupe import (
    _DEDUPE_SYSTEM,
    _build_semantic_batches,
    _claims_match,
    _semantic_merge_is_safe,
    _semantic_dedupe_haiku,
    _semantic_dedupe_pass,
)


def test_claims_match_for_exact_duplicate_contexts() -> None:
    left = {
        "claim_type": "fact",
        "normalized_statement": "the peg was introduced in 1983",
        "normalized_attribution": "",
        "numbers_and_dates": {"1983"},
        "evidence_ids": {"e1"},
        "source_ids": {"s1"},
        "branch_ids": {"b3"},
        "value_tokens": {"peg", "introduced", "1983"},
    }
    right = {
        "claim_type": "fact",
        "normalized_statement": "the peg was introduced in 1983",
        "normalized_attribution": "",
        "numbers_and_dates": {"1983"},
        "evidence_ids": {"e2"},
        "source_ids": {"s2"},
        "branch_ids": {"b3"},
        "value_tokens": {"peg", "introduced", "1983"},
    }

    assert _claims_match(left, right)


def test_claims_match_rejects_same_theme_with_different_numbers() -> None:
    left = {
        "claim_type": "fact",
        "normalized_statement": "reserves reached 430 billion",
        "normalized_attribution": "",
        "numbers_and_dates": {"430"},
        "evidence_ids": {"e1"},
        "source_ids": {"s1"},
        "branch_ids": {"b3"},
        "value_tokens": {"reserves", "reached", "430", "billion"},
    }
    right = {
        "claim_type": "fact",
        "normalized_statement": "reserves reached 420 billion",
        "normalized_attribution": "",
        "numbers_and_dates": {"420"},
        "evidence_ids": {"e2"},
        "source_ids": {"s1"},
        "branch_ids": {"b3"},
        "value_tokens": {"reserves", "reached", "420", "billion"},
    }

    assert not _claims_match(left, right)


def _make_claim(cid: str, statement: str, branch: str = "b1", evidence_count: int = 1):
    """Create a fake claim object for testing."""
    notes = f'__claim_meta__:{{"builder_branch_id": "{branch}", "builder_question_ids": ["{branch}.1"]}}'
    # Use a deterministic UUID derived from the short id for test stability
    fake_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, cid)
    return SimpleNamespace(id=fake_uuid, statement=statement, notes=notes, _short=cid)


def _make_context(cid: str, evidence_count: int = 1):
    return {
        "evidence_ids": {f"e{i}" for i in range(evidence_count)},
        "normalized_statement": cid,
        "numbers_and_dates": set(),
    }


def test_dedupe_system_protects_distinct_historical_stages() -> None:
    assert "problem/precondition vs the institutional answer" in _DEDUPE_SYSTEM
    assert "One regime/state vs the later transition away from it" in _DEDUPE_SYSTEM
    assert "legal rule vs its later application" in _DEDUPE_SYSTEM


def test_build_semantic_batches_uses_overlapping_windows() -> None:
    claims = [_make_claim(f"c{i:02d}", f"claim {i}") for i in range(45)]
    contexts = {str(c.id): _make_context(f"claim-{i:02d}") for i, c in enumerate(claims)}

    batches = _build_semantic_batches(claims, contexts)

    assert len(batches) == 2
    assert len(batches[0]) == 40
    assert len(batches[1]) == 25
    first_batch_ids = {str(claim.id) for claim in batches[0]}
    second_batch_ids = {str(claim.id) for claim in batches[1]}
    assert first_batch_ids & second_batch_ids


def test_semantic_merge_is_safe_rejects_cross_branch_related_but_distinct_claims() -> None:
    keep_id = str(uuid.uuid4())
    drop_id = str(uuid.uuid4())
    contexts = {
        keep_id: {
            "normalized_statement": "banks deposit usd and receive certificates to issue notes",
            "numbers_and_dates": {"7.80"},
            "value_tokens": {"banks", "deposit", "usd", "receive", "certificates", "issue", "notes", "780"},
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e1"},
            "source_ids": {"s1"},
            "branch_ids": {"b1"},
        },
        drop_id: {
            "normalized_statement": "market arbitrage restores the exchange rate around the peg",
            "numbers_and_dates": {"7.80"},
            "value_tokens": {"market", "arbitrage", "restores", "exchange", "rate", "around", "peg", "780"},
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e2"},
            "source_ids": {"s2"},
            "branch_ids": {"b5"},
        },
    }

    assert not _semantic_merge_is_safe(
        keep_id=keep_id,
        drop_id=drop_id,
        contexts=contexts,
        cross_branch=True,
    )


@pytest.mark.asyncio
async def test_semantic_dedupe_haiku_returns_merge_tuples(monkeypatch) -> None:
    """Haiku identifies duplicates → returns (keep, drop, reason) tuples without touching DB."""
    c1 = _make_claim("c1", "CI mechanism requires USD deposit at 7.80", evidence_count=3)
    c2 = _make_claim("c2", "Bank must deposit equivalent USD to issue HKD")
    c3 = _make_claim("c3", "Automatic arbitrage restores the rate to 7.80")

    c1_id, c2_id, c3_id = str(c1.id), str(c2.id), str(c3.id)

    contexts = {
        c1_id: _make_context(c1_id, 3),
        c2_id: _make_context(c2_id, 1),
        c3_id: _make_context(c3_id, 1),
    }

    haiku_response = json.dumps({
        "duplicate_groups": [
            {"keep": c1_id, "drop": [c2_id], "reason": "both describe USD deposit requirement"}
        ]
    })

    async def fake_haiku(system, prompt, max_tokens):
        return haiku_response

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)

    merges = await _semantic_dedupe_haiku(
        label="b1",
        claims_pool=[c1, c2, c3],
        contexts=contexts,
        already_dropped={},
    )

    assert merges == [(c1_id, c2_id, "both describe USD deposit requirement")]


@pytest.mark.asyncio
async def test_semantic_dedupe_haiku_skips_invalid_ids(monkeypatch) -> None:
    """Haiku returns IDs not in the pool → ignored."""
    c1 = _make_claim("c1", "Claim one")
    c2 = _make_claim("c2", "Claim two")
    c3 = _make_claim("c3", "Claim three")

    c1_id = str(c1.id)
    contexts = {str(c.id): _make_context(str(c.id)) for c in [c1, c2, c3]}

    haiku_response = json.dumps({
        "duplicate_groups": [
            {"keep": c1_id, "drop": ["c999"], "reason": "invalid id"}
        ]
    })

    async def fake_haiku(system, prompt, max_tokens):
        return haiku_response

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)

    merges = await _semantic_dedupe_haiku(
        label="b1", claims_pool=[c1, c2, c3],
        contexts=contexts, already_dropped={},
    )
    assert merges == []


@pytest.mark.asyncio
async def test_semantic_dedupe_haiku_survives_failure(monkeypatch) -> None:
    """If Haiku call fails, return empty list instead of crashing."""
    c1 = _make_claim("c1", "Claim one")
    c2 = _make_claim("c2", "Claim two")
    c3 = _make_claim("c3", "Claim three")

    contexts = {str(c.id): _make_context(str(c.id)) for c in [c1, c2, c3]}

    async def fake_haiku(system, prompt, max_tokens):
        raise TimeoutError("Haiku timed out")

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)

    merges = await _semantic_dedupe_haiku(
        label="b1", claims_pool=[c1, c2, c3],
        contexts=contexts, already_dropped={},
    )
    assert merges == []


@pytest.mark.asyncio
async def test_semantic_dedupe_haiku_propagates_step_stopped(monkeypatch) -> None:
    """StepExecutionStopped must propagate, not be swallowed."""
    from app.pipeline.step_control import StepExecutionStopped

    c1 = _make_claim("c1", "Claim one")
    c2 = _make_claim("c2", "Claim two")
    c3 = _make_claim("c3", "Claim three")

    contexts = {str(c.id): _make_context(str(c.id)) for c in [c1, c2, c3]}

    async def fake_haiku(system, prompt, max_tokens):
        raise StepExecutionStopped("stop requested")

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)

    with pytest.raises(StepExecutionStopped):
        await _semantic_dedupe_haiku(
            label="b1", claims_pool=[c1, c2, c3],
            contexts=contexts, already_dropped={},
        )


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_skips_small_branches(monkeypatch) -> None:
    """Branches with fewer than MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE claims are skipped."""
    c1 = _make_claim("c1", "Claim one", branch="b1")
    c2 = _make_claim("c2", "Claim two", branch="b1")

    contexts = {str(c.id): _make_context(str(c.id)) for c in [c1, c2]}

    haiku_called = []

    async def fake_haiku(system, prompt, max_tokens):
        haiku_called.append(True)
        return '{"duplicate_groups": []}'

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)

    dropped = await _semantic_dedupe_pass(
        db=None, surviving_claims=[c1, c2], contexts=contexts,
        duplicate_map={}, merged_into_claim_ids=set(),
    )
    assert dropped == []
    assert haiku_called == []


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_processes_branches_and_cross_branch(monkeypatch) -> None:
    """Claims from different branches get separate Haiku calls, plus a cross-branch call."""
    claims_b1 = [_make_claim(f"b1c{i}", f"B1 claim {i}", branch="b1") for i in range(4)]
    claims_b2 = [_make_claim(f"b2c{i}", f"B2 claim {i}", branch="b2") for i in range(4)]
    all_claims = claims_b1 + claims_b2

    contexts = {str(c.id): _make_context(str(c.id)) for c in all_claims}

    labels_called = []

    async def fake_haiku(system, prompt, max_tokens):
        payload = json.loads(prompt)
        claims = payload["claims"]
        # Cross-branch call has claims from both branches
        branches_in_call = set()
        for c in claims:
            branches_in_call.add("b1" if c["statement"].startswith("B1") else "b2")
        if len(branches_in_call) > 1:
            labels_called.append("_cross_branch")
        else:
            labels_called.append(branches_in_call.pop())
        return '{"duplicate_groups": []}'

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)

    await _semantic_dedupe_pass(
        db=None, surviving_claims=all_claims, contexts=contexts,
        duplicate_map={}, merged_into_claim_ids=set(),
    )
    assert sorted(labels_called) == ["_cross_branch", "b1", "b2"]


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_applies_merges_sequentially(monkeypatch) -> None:
    """Merges from Haiku results are applied via _merge_claims, not in parallel."""
    c1 = _make_claim("c1", "Three banks issue HKD banknotes", branch="b1")
    c2 = _make_claim("c2", "HSBC, BOC, SCB issue HKD notes", branch="b1")
    c3 = _make_claim("c3", "Different claim about something else", branch="b1")

    c1_id, c2_id = str(c1.id), str(c2.id)

    contexts = {str(c.id): _make_context(str(c.id)) for c in [c1, c2, c3]}

    call_count = [0]

    async def fake_haiku(system, prompt, max_tokens):
        call_count[0] += 1
        if call_count[0] == 1:
            # Per-branch call: merge c2 into c1
            return json.dumps({
                "duplicate_groups": [
                    {"keep": c1_id, "drop": [c2_id], "reason": "same fact about three banks"}
                ]
            })
        # Cross-branch call: no duplicates
        return '{"duplicate_groups": []}'

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)

    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()

    dropped = await _semantic_dedupe_pass(
        db=None, surviving_claims=[c1, c2, c3], contexts=contexts,
        duplicate_map=duplicate_map, merged_into_claim_ids=merged_into,
    )

    assert c2_id in dropped
    assert duplicate_map[c2_id] == c1_id
    assert c1_id in merged_into
    assert merge_log == [(c1_id, c2_id)]
