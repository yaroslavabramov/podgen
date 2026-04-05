from types import SimpleNamespace
import uuid

import pytest

from app.models.claim import VerificationLevel
from app.pipeline.phase4_claims.verifier import (
    VERIFY_BATCH_CHAR_BUDGET,
    _apply_verification_ceiling,
    _build_verifier_claim_payload,
    _build_research_gaps,
    _compute_verification_fingerprint,
    _derive_verify_scope,
    _estimate_verifier_payload_chars,
    _index_provenance_asset_types,
    _index_provenance_contexts,
    _is_meaningful_attribution,
    _rule_based_fallback,
    _split_verifier_batches,
    verify_claims,
)


def test_verification_ceiling_blocks_verified_for_temporary_only_support() -> None:
    evidence_data = {
        "supporting": [
            {
                "authority_type": "primary",
                "source_reliability": "high",
                "is_temporary": True,
            }
        ],
        "source_ids": {"source-1"},
        "all_supporting_temporary": True,
        "has_counter": False,
    }

    assert _apply_verification_ceiling(VerificationLevel.VERIFIED, evidence_data) == VerificationLevel.OBSERVED


def test_rule_based_fallback_requires_two_sources_for_verified() -> None:
    evidence_data = {
        "supporting": [
            {
                "authority_type": "primary",
                "source_reliability": "high",
                "is_temporary": False,
            }
        ],
        "source_ids": {"source-1"},
        "all_supporting_temporary": False,
        "has_counter": False,
    }

    assert _rule_based_fallback(evidence_data) == VerificationLevel.OBSERVED


def test_meaningful_attribution_rejects_placeholder() -> None:
    assert not _is_meaningful_attribution({"who": "неустановленный эксперт"})
    assert _is_meaningful_attribution({"who": "Paul Volcker"})


def test_index_provenance_asset_types_prefers_contextual_types() -> None:
    evidence_id = uuid.uuid4()
    indexed = _index_provenance_asset_types(
        [
            SimpleNamespace(evidence_id=evidence_id, asset_type="mechanism"),
            SimpleNamespace(evidence_id=evidence_id, asset_type="statistic"),
        ]
    )

    assert indexed[evidence_id] == ["MECHANISM", "STATISTIC"]


def test_build_research_gaps_preserves_actual_provenance_pairs() -> None:
    claim = SimpleNamespace(id=uuid.uuid4(), statement="Explain the bridge role")
    gaps = _build_research_gaps(
        [
            (
                claim,
                {
                    "provenance_pairs": [
                        {
                            "branch_id": "b1",
                            "question_id": "q1",
                            "question": "How does it work?",
                            "intent": "explain",
                            "found_assets": ["MECHANISM"],
                        },
                        {
                            "branch_id": "b2",
                            "question_id": "q2",
                            "question": "Why does it matter?",
                            "intent": "debate",
                            "found_assets": ["TIMELINE"],
                        },
                    ],
                    "found_assets": ["MECHANISM", "TIMELINE"],
                },
            )
        ],
        question_context_map={
            "q1": {
                "question": "How does it work?",
                "intent": "explain",
                "required_evidence": [{"type": "MECHANISM", "min_count": 1}],
                "expected_evidence": ["TIMELINE"],
            },
            "q2": {
                "question": "Why does it matter?",
                "intent": "debate",
                "required_evidence": [{"type": "CONTRAST", "min_count": 1}],
                "expected_evidence": ["CASE"],
            },
        },
    )

    assert len(gaps) == 1
    assert (gaps[0]["branch_id"], gaps[0]["question_id"]) == ("b1", "q1")
    assert gaps[0]["question"] == "How does it work?"
    assert gaps[0]["intent"] == "explain"
    assert set(gaps[0]["found_assets"]) == {"MECHANISM", "TIMELINE"}
    assert gaps[0]["required_assets"] == [
        {"type": "CONTRAST", "min_count": 1},
        {"type": "MECHANISM", "min_count": 1},
    ]
    assert gaps[0]["expected_assets"] == ["CASE", "TIMELINE"]
    assert gaps[0]["gap_origin"] == "verifier"
    assert gaps[0]["claim_statement"] == "Explain the bridge role"
    assert gaps[0]["support_strength"] == "single_source"
    assert "independent second source" in gaps[0]["search_objective"]
    assert gaps[0]["claim_contexts"] == [
        {
            "claim_id": str(claim.id),
            "claim_statement": "Explain the bridge role",
            "support_strength": "single_source",
            "search_objective": "Find an independent second source that confirms: Explain the bridge role",
            "existing_support_summary": "single_source; 0 source(s); 0 direct / 0 contextual; authority=unknown-authority",
        }
    ]
    assert {ctx["question_id"] for ctx in gaps[0]["question_contexts"]} == {"q1", "q2"}


def test_build_research_gaps_aggregates_multiple_claim_contexts_for_same_pair() -> None:
    claim_one = SimpleNamespace(id=uuid.uuid4(), statement="Claim one")
    claim_two = SimpleNamespace(id=uuid.uuid4(), statement="Claim two")

    gaps = _build_research_gaps(
        [
            (
                claim_one,
                {
                    "provenance_pairs": [
                        {
                            "branch_id": "b6",
                            "question_id": "q6",
                            "question": "Why did the system survive?",
                            "intent": "debate",
                            "found_assets": ["MECHANISM"],
                        }
                    ],
                    "found_assets": ["MECHANISM"],
                    "source_ids": {"source-1"},
                    "supporting": [{"authority_type": "secondary"}],
                    "direct_evidence_count": 1,
                    "contextual_evidence_count": 0,
                },
            ),
            (
                claim_two,
                {
                    "provenance_pairs": [
                        {
                            "branch_id": "b6",
                            "question_id": "q6",
                            "question": "Why did the system survive?",
                            "intent": "debate",
                            "found_assets": ["CONTRAST"],
                        }
                    ],
                    "found_assets": ["CONTRAST"],
                    "source_ids": {"source-2"},
                    "supporting": [{"authority_type": "tertiary"}],
                    "direct_evidence_count": 0,
                    "contextual_evidence_count": 1,
                },
            ),
        ],
        question_context_map={
            "q6": {
                "question": "Why did the system survive?",
                "intent": "debate",
                "required_evidence": [{"type": "CONTRAST", "min_count": 1}],
                "expected_evidence": ["CASE"],
            }
        },
    )

    assert len(gaps) == 1
    assert set(gaps[0]["claim_ids"]) == {str(claim_one.id), str(claim_two.id)}
    assert {context["claim_id"] for context in gaps[0]["claim_contexts"]} == {str(claim_one.id), str(claim_two.id)}
    assert "Claim one" in gaps[0]["claim_statement"]
    assert "Claim two" in gaps[0]["claim_statement"]
    assert "Find an independent second source that confirms: Claim one" in gaps[0]["search_objective"]
    assert "Find a direct source that explicitly confirms: Claim two" in gaps[0]["search_objective"]


def test_build_research_gaps_merges_overlapping_claim_groups_by_union_of_claim_ids() -> None:
    shared_claim = SimpleNamespace(id=uuid.uuid4(), statement="Shared claim")
    extra_claim = SimpleNamespace(id=uuid.uuid4(), statement="Extra claim")

    gaps = _build_research_gaps(
        [
            (
                shared_claim,
                {
                    "provenance_pairs": [
                        {
                            "branch_id": "b6",
                            "question_id": "q62",
                            "question": "Why did three-bank issuance persist?",
                            "intent": "debate",
                            "found_assets": ["CONTRAST"],
                        }
                    ],
                    "found_assets": ["CONTRAST"],
                    "source_ids": {"source-1"},
                    "supporting": [{"authority_type": "secondary"}],
                    "direct_evidence_count": 1,
                    "contextual_evidence_count": 0,
                },
            ),
            (
                shared_claim,
                {
                    "provenance_pairs": [
                        {
                            "branch_id": "b6",
                            "question_id": "q63",
                            "question": "What constraints did the system create?",
                            "intent": "debate",
                            "found_assets": ["MECHANISM"],
                        }
                    ],
                    "found_assets": ["MECHANISM"],
                    "source_ids": {"source-1"},
                    "supporting": [{"authority_type": "secondary"}],
                    "direct_evidence_count": 1,
                    "contextual_evidence_count": 0,
                },
            ),
            (
                extra_claim,
                {
                    "provenance_pairs": [
                        {
                            "branch_id": "b6",
                            "question_id": "q63",
                            "question": "What constraints did the system create?",
                            "intent": "debate",
                            "found_assets": ["MECHANISM"],
                        }
                    ],
                    "found_assets": ["MECHANISM"],
                    "source_ids": {"source-2"},
                    "supporting": [{"authority_type": "primary"}],
                    "direct_evidence_count": 1,
                    "contextual_evidence_count": 0,
                },
            ),
        ],
        question_context_map={
            "q62": {
                "question": "Why did three-bank issuance persist?",
                "intent": "debate",
                "required_evidence": [{"type": "CONTRAST", "min_count": 1}],
                "expected_evidence": ["CASE"],
            },
            "q63": {
                "question": "What constraints did the system create?",
                "intent": "debate",
                "required_evidence": [{"type": "MECHANISM", "min_count": 1}],
                "expected_evidence": ["CONTRAST"],
            },
        },
    )

    assert len(gaps) == 1
    assert set(gaps[0]["claim_ids"]) == {str(shared_claim.id), str(extra_claim.id)}
    assert {ctx["question_id"] for ctx in gaps[0]["question_contexts"]} == {"q62", "q63"}
    assert set(gaps[0]["found_assets"]) == {"CONTRAST", "MECHANISM"}


def test_derive_verify_scope_prefers_rebuilt_and_merged_claims_after_first_loop() -> None:
    scope = _derive_verify_scope(
        {
            "iteration_counts": {"verify_research_loops": 1},
            "rebuilt_claim_ids": ["c2", "c3"],
            "dedupe_merged_into_claim_ids": ["c1"],
            "research_gaps": [{"claim_id": "c4", "claim_ids": ["c4", "c5"]}],
        },
        ["c1", "c2", "c3", "c4", "c5"],
    )

    assert scope == ["c1", "c2", "c3", "c4", "c5"]


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeVerifySession:
    def __init__(self, claim):
        self._claim = claim
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        return _FakeScalarResult(self._claim)

    async def commit(self):
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_verify_claims_skips_unchanged_gap_claim_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_id = str(uuid.uuid4())
    claim = SimpleNamespace(
        id=uuid.UUID(claim_id),
        claim_type="fact",
        statement="The colonial government delegated note issuance to private banks.",
        verification_level=VerificationLevel.OBSERVED,
        verification_fingerprint="",
    )
    evidence_data = {
        "supporting": [
            {
                "evidence_id": str(uuid.uuid4()),
                "quote": "The colonial government delegated note issuance to private banks under an ordinance.",
                "asset_type": "mechanism",
                "asset_types": ["MECHANISM"],
                "source_reliability": "medium",
                "authority_type": "secondary",
                "is_temporary": False,
                "source_id": "source-1",
                "canonical_source_id": "source-1",
                "direct_or_contextual": ["direct"],
            }
        ],
        "has_counter": False,
        "source_ids": {"source-1"},
        "all_supporting_temporary": False,
        "branch_ids": ["b1"],
        "question_ids": ["q1"],
        "intents": ["explain"],
        "provenance_pairs": [
            {
                "branch_id": "b1",
                "question_id": "q1",
                "question": "How did it work?",
                "intent": "explain",
                "found_assets": ["MECHANISM"],
                "direct_count": 1,
                "contextual_count": 0,
            }
        ],
        "found_assets": ["MECHANISM"],
        "direct_evidence_count": 1,
        "contextual_evidence_count": 0,
    }
    claim.verification_fingerprint = _compute_verification_fingerprint(claim, evidence_data)

    session = _FakeVerifySession(claim)
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.async_session_factory",
        lambda: session,
    )
    async def _fake_load_claim_evidence(*args, **kwargs):
        return evidence_data

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier._load_claim_evidence",
        _fake_load_claim_evidence,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.update_generation_progress",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier._dump_verifier_debug",
        lambda *args, **kwargs: None,
    )

    async def _llm_should_not_run(*args, **kwargs):
        raise AssertionError("LLM verifier should not run for unchanged claim payload")

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier._llm_verify_batch",
        _llm_should_not_run,
    )

    result = await verify_claims(
        {
            "generation_run_id": str(uuid.uuid4()),
            "manifest_id": "",
            "claim_ids": [claim_id],
            "iteration_counts": {"verify_research_loops": 1},
            "research_gaps": [
                {
                    "claim_id": claim_id,
                    "branch_id": "b1",
                    "question_id": "q1",
                    "question": "How did it work?",
                    "intent": "explain",
                    "gap_origin": "verifier",
                }
            ],
            "rebuilt_claim_ids": [claim_id],
            "dedupe_merged_into_claim_ids": [],
            "preserved_claim_ids": [],
            "targeted_branch_ids": ["b1"],
            "dedupe_deleted_claim_ids": [],
            "dedupe_untouched_claim_ids": [],
        }
    )

    assert session.commit_calls == 1
    assert claim.verification_level == VerificationLevel.OBSERVED
    assert result["research_gaps"][0]["claim_id"] == claim_id
    assert result["rebuilt_claim_ids"] == []
    assert result["dedupe_merged_into_claim_ids"] == []


def test_index_provenance_contexts_attaches_intent_and_directness() -> None:
    evidence_id = uuid.uuid4()
    contexts_by_evidence, pairs = _index_provenance_contexts(
        [
            SimpleNamespace(
                evidence_id=evidence_id,
                asset_type="mechanism",
                branch_id="b1",
                question_id="q1",
                direct_or_contextual="direct",
            ),
            SimpleNamespace(
                evidence_id=evidence_id,
                asset_type="case",
                branch_id="b1",
                question_id="q1",
                direct_or_contextual="contextual",
            ),
        ],
        question_context_map={"q1": {"question": "How does it work?", "intent": "explain"}},
    )

    assert contexts_by_evidence[evidence_id][0]["intent"] == "explain"
    assert {item["direct_or_contextual"] for item in contexts_by_evidence[evidence_id]} == {"direct", "contextual"}
    assert pairs == [
        {
            "branch_id": "b1",
            "question_id": "q1",
            "question": "How does it work?",
            "intent": "explain",
            "found_assets": ["CASE", "MECHANISM"],
            "direct_count": 1,
            "contextual_count": 1,
        }
    ]


def test_build_verifier_claim_payload_keeps_full_quote_text() -> None:
    long_quote = "A" * 1200
    long_statement = "Claim statement " * 60
    claim = SimpleNamespace(id=uuid.uuid4(), statement=long_statement)
    payload = _build_verifier_claim_payload(
        claim,
        {
            "supporting": [{"quote": long_quote}],
            "source_ids": {"source-1"},
            "branch_ids": ["b1"],
            "question_ids": ["q1"],
            "intents": ["history"],
            "direct_evidence_count": 1,
            "contextual_evidence_count": 0,
            "provenance_pairs": [],
        },
    )

    assert payload["evidence"][0]["quote"] == long_quote
    assert payload["statement"] == long_statement


def test_split_verifier_batches_uses_char_budget() -> None:
    claims_for_llm = [
        (
            SimpleNamespace(id=uuid.uuid4(), statement="Claim 1"),
            {
                "supporting": [{"quote": "A" * 10000}],
                "source_ids": {"source-1"},
                "branch_ids": ["b1"],
                "question_ids": ["q1"],
                "intents": ["history"],
                "direct_evidence_count": 1,
                "contextual_evidence_count": 0,
                "provenance_pairs": [],
            },
        ),
        (
            SimpleNamespace(id=uuid.uuid4(), statement="Claim 2"),
            {
                "supporting": [{"quote": "B" * 10000}],
                "source_ids": {"source-2"},
                "branch_ids": ["b2"],
                "question_ids": ["q2"],
                "intents": ["explain"],
                "direct_evidence_count": 1,
                "contextual_evidence_count": 0,
                "provenance_pairs": [],
            },
        ),
    ]

    batches = _split_verifier_batches(claims_for_llm)

    assert len(batches) == 2
    assert [item[0].statement for item in batches[0]] == ["Claim 1"]
    assert [item[0].statement for item in batches[1]] == ["Claim 2"]


def test_build_verifier_claim_payload_clips_oversized_single_claim_to_budget() -> None:
    huge_quote = "Q" * 50000
    claim = SimpleNamespace(id=uuid.uuid4(), statement="Claim statement")

    payload = _build_verifier_claim_payload(
        claim,
        {
            "supporting": [{"quote": huge_quote, "asset_type": "mechanism"}],
            "source_ids": {"source-1"},
            "branch_ids": ["b1"],
            "question_ids": ["q1"],
            "intents": ["explain"],
            "direct_evidence_count": 1,
            "contextual_evidence_count": 0,
            "provenance_pairs": [],
        },
    )

    assert len(payload["evidence"][0]["quote"]) < len(huge_quote)
    assert "excerpt clipped for verifier budget" in payload["evidence"][0]["quote"]
    assert len(payload["evidence"][0]["quote"]) < VERIFY_BATCH_CHAR_BUDGET
    assert _estimate_verifier_payload_chars(payload) <= VERIFY_BATCH_CHAR_BUDGET


def test_build_verifier_claim_payload_trims_metadata_heavy_claim_to_budget() -> None:
    claim = SimpleNamespace(id=uuid.uuid4(), statement="Claim statement")
    supporting = []
    for index in range(24):
        supporting.append(
            {
                "quote": f"Quote {index} " + ("Q" * 400),
                "asset_type": "mechanism",
                "asset_types": ["MECHANISM", "CASE"],
                "source_reliability": "medium",
                "authority_type": "secondary",
                "is_temporary": False,
                "canonical_source_id": f"source-{index}",
                "direct_or_contextual": ["direct" if index % 2 == 0 else "contextual"],
            }
        )
    provenance_pairs = []
    for index in range(40):
        provenance_pairs.append(
            {
                "branch_id": f"b{index}",
                "question_id": f"q{index}",
                "intent": "debate" if index % 2 else "compare",
                "found_assets": ["MECHANISM", "CONTRAST", "CASE"],
                "direct_count": 1,
                "contextual_count": 2,
            }
        )

    payload = _build_verifier_claim_payload(
        claim,
        {
            "supporting": supporting,
            "source_ids": {f"source-{index}" for index in range(24)},
            "branch_ids": [f"b{index}" for index in range(12)],
            "question_ids": [f"q{index}" for index in range(12)],
            "intents": ["debate", "compare"],
            "direct_evidence_count": 12,
            "contextual_evidence_count": 12,
            "provenance_pairs": provenance_pairs,
        },
    )

    assert payload.get("supporting_evidence_count") == 24
    assert payload.get("provenance_pair_count") == 40
    assert payload.get("omitted_supporting_evidence_count", 0) > 0
    assert payload.get("omitted_provenance_pair_count", 0) > 0
    assert _estimate_verifier_payload_chars(payload) <= VERIFY_BATCH_CHAR_BUDGET


def test_build_verifier_claim_payload_trims_large_top_level_context_lists() -> None:
    claim = SimpleNamespace(id=uuid.uuid4(), statement="Claim statement")
    long_branch_ids = [f"branch-{idx}-" + ("x" * 180) for idx in range(200)]
    long_question_ids = [f"question-{idx}-" + ("y" * 180) for idx in range(200)]
    long_intents = [f"intent-{idx}-" + ("z" * 80) for idx in range(20)]
    payload = _build_verifier_claim_payload(
        claim,
        {
            "supporting": [
                {
                    "quote": "Short quote",
                    "asset_type": "mechanism",
                    "asset_types": ["MECHANISM"],
                    "source_reliability": "high",
                    "authority_type": "primary",
                    "is_temporary": False,
                    "canonical_source_id": "source-1",
                    "direct_or_contextual": ["direct"],
                }
            ],
            "source_ids": {"source-1"},
            "branch_ids": long_branch_ids,
            "question_ids": long_question_ids,
            "intents": long_intents,
            "direct_evidence_count": 1,
            "contextual_evidence_count": 0,
            "provenance_pairs": [],
        },
    )

    assert payload.get("omitted_branch_id_count", 0) > 0
    assert payload.get("omitted_question_id_count", 0) > 0
    assert payload.get("omitted_intent_count", 0) > 0
    assert _estimate_verifier_payload_chars(payload) <= VERIFY_BATCH_CHAR_BUDGET
