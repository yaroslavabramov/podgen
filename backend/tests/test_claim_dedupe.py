import json
import uuid
from types import SimpleNamespace

import pytest

from app.pipeline.phase4_claims.dedupe import (
    _DEDUPE_SYSTEM,
    _build_semantic_batches,
    _claims_match,
    _consolidate_cross_branch_suggestions,
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


def _make_context(
    cid: str,
    evidence_count: int = 1,
    *,
    claim_type: str = "fact",
    numbers_and_dates: set[str] | None = None,
):
    return {
        "evidence_ids": {f"e{i}" for i in range(evidence_count)},
        "normalized_statement": cid,
        "numbers_and_dates": set(numbers_and_dates or set()),
        "claim_type": claim_type,
        "normalized_attribution": "",
        "value_tokens": set(),
        "source_ids": set(),
        "branch_ids": set(),
    }


def test_dedupe_system_protects_distinct_historical_stages() -> None:
    assert "genuinely interchangeable" in _DEDUPE_SYSTEM
    assert "Mutual-substitutability test" in _DEDUPE_SYSTEM
    assert "proposal / recommendation / plan vs official adoption / enactment / implementation" in _DEDUPE_SYSTEM
    assert "baseline fact vs comparison / contrast" in _DEDUPE_SYSTEM
    assert '"same event"' in _DEDUPE_SYSTEM
    assert "false negatives are much better than false positives" in _DEDUPE_SYSTEM.lower()


def test_consolidate_cross_branch_suggestions_resolves_opposite_directions() -> None:
    left = _make_claim("c1", "A richer statement", branch="b1", evidence_count=3)
    right = _make_claim("c2", "A shorter statement", branch="b4", evidence_count=1)
    left_id, right_id = str(left.id), str(right.id)
    contexts = {
        left_id: {
            "evidence_ids": {"e1", "e2", "e3"},
            "normalized_statement": "a richer statement with more detail",
        },
        right_id: {
            "evidence_ids": {"e4"},
            "normalized_statement": "a shorter statement",
        },
    }

    consolidated = _consolidate_cross_branch_suggestions(
        [
            {"batch_index": 0, "keep_id": left_id, "drop_id": right_id, "reason": "same proposition"},
            {"batch_index": 1, "keep_id": right_id, "drop_id": left_id, "reason": "same proposition"},
        ],
        contexts,
    )

    assert len(consolidated) == 1
    assert consolidated[0]["keep_id"] == left_id
    assert consolidated[0]["drop_id"] == right_id
    assert consolidated[0]["direction_conflict"] is True
    assert consolidated[0]["batch_indexes"] == [0, 1]
    assert consolidated[0]["suggestion_count"] == 2


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


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_accepts_safe_cross_branch_merge(monkeypatch) -> None:
    c1 = _make_claim("c1", "1935 ordinance made notes over 5 dollars legal tender via three authorized banks", branch="b1")
    c2 = _make_claim("c2", "In 1935 the ordinance granted three banks the right to issue notes over 5 dollars as legal tender", branch="b4")
    c3 = _make_claim("c3", "Completely different claim", branch="b1")
    c4 = _make_claim("c4", "Another different claim", branch="b4")
    c1_id, c2_id = str(c1.id), str(c2.id)

    contexts = {
        c1_id: {
            "normalized_statement": "the 1935 ordinance made notes over 5 dollars legal tender issued by three authorized banks",
            "numbers_and_dates": {"1935", "5"},
            "value_tokens": {
                "1935", "ordinance", "made", "notes", "over", "dollars", "legal", "tender",
                "issued", "three", "authorized", "banks",
            },
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e1"},
            "source_ids": {"s1"},
            "branch_ids": {"b1"},
        },
        c2_id: {
            "normalized_statement": "in 1935 the ordinance legally granted three banks the right to issue notes over 5 dollars as legal tender",
            "numbers_and_dates": {"1935", "5"},
            "value_tokens": {
                "1935", "ordinance", "legally", "granted", "three", "banks", "right", "issue",
                "notes", "over", "dollars", "legal", "tender",
            },
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e2"},
            "source_ids": {"s2"},
            "branch_ids": {"b4"},
        },
        str(c3.id): _make_context("different-a"),
        str(c4.id): _make_context("different-b"),
    }

    async def fake_haiku(system, prompt, max_tokens):
        payload = json.loads(prompt)
        statements = [c["statement"] for c in payload["claims"]]
        if any("1935 ordinance" in s.lower() for s in statements) and any("different" in s.lower() for s in statements):
            claims = payload["claims"]
            by_statement = {c["statement"]: c["id"] for c in claims}
            return json.dumps({
                "duplicate_groups": [{
                    "keep": by_statement[c1.statement],
                    "drop": [by_statement[c2.statement]],
                    "reason": "same legal-tender proposition"
                }]
            })
        return '{"duplicate_groups": []}'

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)

    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()
    branch_log: list[dict] = []

    dropped = await _semantic_dedupe_pass(
        db=None,
        surviving_claims=[c1, c3, c2, c4],
        contexts=contexts,
        duplicate_map=duplicate_map,
        merged_into_claim_ids=merged_into,
        branch_log=branch_log,
    )

    assert c2_id in dropped
    assert duplicate_map[c2_id] == c1_id
    assert c1_id in merged_into
    assert merge_log == [(c1_id, c2_id)]
    cross_branch = next(item for item in branch_log if item["branch_id"] == "_cross_branch")
    assert cross_branch["suggested_merge_count"] == 1
    assert cross_branch["applied_merge_count"] == 1
    assert cross_branch["skipped_merge_count"] == 0
    assert cross_branch["suggested_merges"][0]["keep_id"] == c1_id
    assert cross_branch["suggested_merges"][0]["drop_id"] == c2_id
    assert cross_branch["applied_merges"][0]["keep_id"] == c1_id
    assert cross_branch["applied_merges"][0]["drop_id"] == c2_id


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_consolidates_duplicate_cross_branch_suggestions(monkeypatch) -> None:
    c1 = _make_claim("c1", "Core claim", branch="b1")
    c2 = _make_claim("c2", "Duplicate claim", branch="b4")
    c3 = _make_claim("c3", "Different claim", branch="b1")
    c4 = _make_claim("c4", "Another different claim", branch="b4")
    c1_id, c2_id = str(c1.id), str(c2.id)

    contexts = {
        str(c.id): {
            "normalized_statement": c.statement.lower(),
            "numbers_and_dates": set(),
            "value_tokens": set(c.statement.lower().split()),
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {f"e-{i}"},
            "source_ids": {f"s-{i}"},
            "branch_ids": {c.notes.split('builder_branch_id": "')[1].split('"')[0]},
        }
        for i, c in enumerate([c1, c2, c3, c4], start=1)
    }

    async def fake_haiku(system, prompt, max_tokens):
        payload = json.loads(prompt)
        by_statement = {c["statement"]: c["id"] for c in payload["claims"]}
        if c1.statement in by_statement and c2.statement in by_statement:
            return json.dumps({
                "duplicate_groups": [{
                    "keep": by_statement[c1.statement],
                    "drop": [by_statement[c2.statement]],
                    "reason": "same proposition"
                }]
            })
        return '{"duplicate_groups": []}'

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe.call_haiku_structured", fake_haiku)
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._build_semantic_batches",
        lambda claims, contexts: [[c1, c2, c3], [c1, c2, c4]],
    )

    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()
    branch_log: list[dict] = []

    dropped = await _semantic_dedupe_pass(
        db=None,
        surviving_claims=[c1, c3, c2, c4],
        contexts=contexts,
        duplicate_map=duplicate_map,
        merged_into_claim_ids=merged_into,
        branch_log=branch_log,
    )

    assert dropped == [c2_id]
    assert merge_log == [(c1_id, c2_id)]
    cross_branch = next(item for item in branch_log if item["branch_id"] == "_cross_branch")
    assert cross_branch["suggested_merge_count"] == 2
    assert cross_branch["consolidated_merge_count"] == 1
    assert cross_branch["applied_merge_count"] == 1
    assert cross_branch["skipped_merge_count"] == 0


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_consolidates_opposite_direction_cross_branch_merges(monkeypatch) -> None:
    c1 = _make_claim("c1", "Core proposition with more support", branch="b1")
    c2 = _make_claim("c2", "Core proposition restated", branch="b4")
    c3 = _make_claim("c3", "Branch filler one", branch="b1")
    c4 = _make_claim("c4", "Branch filler two", branch="b4")
    c1_id, c2_id = str(c1.id), str(c2.id)

    contexts = {
        c1_id: {
            "normalized_statement": "core proposition with more support and wording",
            "numbers_and_dates": set(),
            "value_tokens": {"core", "proposition", "support", "wording"},
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e1", "e2", "e3"},
            "source_ids": {"s1"},
            "branch_ids": {"b1"},
        },
        c2_id: {
            "normalized_statement": "core proposition restated",
            "numbers_and_dates": set(),
            "value_tokens": {"core", "proposition", "restated"},
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e4"},
            "source_ids": {"s2"},
            "branch_ids": {"b4"},
        },
        str(c3.id): _make_context("filler-a"),
        str(c4.id): _make_context("filler-b"),
    }

    async def fake_semantic_haiku(label, claims_pool, contexts, already_dropped):
        if label == "_cross_branch_0":
            return [(c1_id, c2_id, "same proposition")]
        if label == "_cross_branch_1":
            return [(c2_id, c1_id, "same proposition")]
        return []

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._semantic_dedupe_haiku",
        fake_semantic_haiku,
    )
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.dedupe._build_semantic_batches",
        lambda claims, contexts: [[c1, c2, c3], [c1, c2, c4]] if len(claims) == 4 else [],
    )

    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()
    branch_log: list[dict] = []

    dropped = await _semantic_dedupe_pass(
        db=None,
        surviving_claims=[c1, c3, c2, c4],
        contexts=contexts,
        duplicate_map=duplicate_map,
        merged_into_claim_ids=merged_into,
        branch_log=branch_log,
    )

    assert dropped == [c2_id]
    assert merge_log == [(c1_id, c2_id)]
    assert duplicate_map == {c2_id: c1_id}
    cross_branch = next(item for item in branch_log if item["branch_id"] == "_cross_branch")
    assert cross_branch["suggested_merge_count"] == 2
    assert cross_branch["consolidated_merge_count"] == 1
    assert cross_branch["applied_merge_count"] == 1
    assert cross_branch["skipped_merge_count"] == 0
    assert cross_branch["consolidated_merges"][0]["direction_conflict"] is True


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_skips_cross_branch_merge_on_numbers_and_dates_mismatch(monkeypatch) -> None:
    # 2 claims per branch (< MIN_BRANCH_CLAIMS_FOR_SEMANTIC_DEDUPE) → per-branch skipped, cross-branch runs on total pool.
    keep = _make_claim("keep", "Policy adopted in 2019", branch="b1")
    drop = _make_claim("drop", "Policy adopted in 2020", branch="b2")
    filler_1 = _make_claim("f1", "Filler one", branch="b1")
    filler_2 = _make_claim("f2", "Filler two", branch="b2")

    keep_id, drop_id = str(keep.id), str(drop.id)
    contexts = {
        keep_id: {
            "normalized_statement": keep.statement.lower(),
            "numbers_and_dates": {"2019"},
            "value_tokens": {"policy", "adopted", "2019"},
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e1"},
            "source_ids": {"s1"},
            "branch_ids": {"b1"},
        },
        drop_id: {
            "normalized_statement": drop.statement.lower(),
            "numbers_and_dates": {"2020"},
            "value_tokens": {"policy", "adopted", "2020"},
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e2"},
            "source_ids": {"s2"},
            "branch_ids": {"b2"},
        },
        str(filler_1.id): _make_context("filler-1"),
        str(filler_2.id): _make_context("filler-2"),
    }

    async def fake_semantic_haiku(label, claims_pool, contexts, already_dropped):
        if label == "_cross_branch_0":
            return [(keep_id, drop_id, "same proposition")]
        return []

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._semantic_dedupe_haiku", fake_semantic_haiku)
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)

    branch_log: list[dict] = []
    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()

    dropped = await _semantic_dedupe_pass(
        db=None,
        surviving_claims=[keep, filler_1, drop, filler_2],
        contexts=contexts,
        duplicate_map=duplicate_map,
        merged_into_claim_ids=merged_into,
        branch_log=branch_log,
    )

    assert dropped == []
    assert merge_log == []
    cross_branch = next(item for item in branch_log if item["branch_id"] == "_cross_branch")
    assert cross_branch["suggested_merge_count"] == 1
    assert cross_branch["consolidated_merge_count"] == 1
    assert cross_branch["applied_merge_count"] == 0
    assert cross_branch["skipped_merge_count"] == 1
    assert cross_branch["skipped_merges"][0]["skip_reason"] == "numbers_and_dates_mismatch"


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_skips_cross_branch_merge_on_claim_type_mismatch(monkeypatch) -> None:
    keep = _make_claim("keep", "The committee adopted the policy", branch="b1")
    drop = _make_claim("drop", "The committee adopted the policy (author said so)", branch="b2")
    filler_1 = _make_claim("f1", "Filler one", branch="b1")
    filler_2 = _make_claim("f2", "Filler two", branch="b2")

    keep_id, drop_id = str(keep.id), str(drop.id)
    contexts = {
        keep_id: {
            "normalized_statement": keep.statement.lower(),
            "numbers_and_dates": set(),
            "value_tokens": set(keep.statement.lower().split()),
            "claim_type": "fact",
            "normalized_attribution": "",
            "evidence_ids": {"e1"},
            "source_ids": {"s1"},
            "branch_ids": {"b1"},
        },
        drop_id: {
            "normalized_statement": drop.statement.lower(),
            "numbers_and_dates": set(),
            "value_tokens": set(drop.statement.lower().split()),
            "claim_type": "opinion",
            "normalized_attribution": "",
            "evidence_ids": {"e2"},
            "source_ids": {"s2"},
            "branch_ids": {"b2"},
        },
        str(filler_1.id): _make_context("filler-1"),
        str(filler_2.id): _make_context("filler-2"),
    }

    async def fake_semantic_haiku(label, claims_pool, contexts, already_dropped):
        if label == "_cross_branch_0":
            return [(keep_id, drop_id, "same proposition")]
        return []

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._semantic_dedupe_haiku", fake_semantic_haiku)
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)

    branch_log: list[dict] = []
    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()

    dropped = await _semantic_dedupe_pass(
        db=None,
        surviving_claims=[keep, filler_1, drop, filler_2],
        contexts=contexts,
        duplicate_map=duplicate_map,
        merged_into_claim_ids=merged_into,
        branch_log=branch_log,
    )

    assert dropped == []
    assert merge_log == []
    cross_branch = next(item for item in branch_log if item["branch_id"] == "_cross_branch")
    assert cross_branch["suggested_merge_count"] == 1
    assert cross_branch["consolidated_merge_count"] == 1
    assert cross_branch["applied_merge_count"] == 0
    assert cross_branch["skipped_merge_count"] == 1
    assert cross_branch["skipped_merges"][0]["skip_reason"] == "claim_type_mismatch"


@pytest.mark.asyncio
async def test_semantic_dedupe_pass_skips_per_branch_merge_on_numbers_and_dates_mismatch(monkeypatch) -> None:
    keep = _make_claim("keep", "Policy adopted in 2019", branch="b1")
    drop = _make_claim("drop", "Policy adopted in 2020", branch="b1")
    filler = _make_claim("f1", "Filler one", branch="b1")

    keep_id, drop_id = str(keep.id), str(drop.id)
    contexts = {
        keep_id: _make_context(keep.statement.lower(), numbers_and_dates={"2019"}),
        drop_id: _make_context(drop.statement.lower(), numbers_and_dates={"2020"}),
        str(filler.id): _make_context(filler.statement.lower()),
    }

    async def fake_semantic_haiku(label, claims_pool, contexts, already_dropped):
        if label == "b1":
            return [(keep_id, drop_id, "same proposition")]
        return []

    merge_log = []

    async def fake_merge(db, *, keep_claim_id, drop_claim_id):
        merge_log.append((str(keep_claim_id), str(drop_claim_id)))

    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._semantic_dedupe_haiku", fake_semantic_haiku)
    monkeypatch.setattr("app.pipeline.phase4_claims.dedupe._merge_claims", fake_merge)

    branch_log: list[dict] = []
    duplicate_map: dict[str, str] = {}
    merged_into: set[str] = set()

    dropped = await _semantic_dedupe_pass(
        db=None,
        surviving_claims=[keep, drop, filler],
        contexts=contexts,
        duplicate_map=duplicate_map,
        merged_into_claim_ids=merged_into,
        branch_log=branch_log,
    )

    assert dropped == []
    assert merge_log == []
    b1 = next(item for item in branch_log if item["branch_id"] == "b1")
    assert b1["suggested_merge_count"] == 1
    assert b1["applied_merge_count"] == 0
    assert b1["skipped_merge_count"] == 1
    assert b1["skipped_merges"][0]["skip_reason"] == "numbers_and_dates_mismatch"
