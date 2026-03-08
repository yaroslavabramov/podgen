from app.pipeline.phase4_claims.dedupe import _claims_match


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
