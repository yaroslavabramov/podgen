from types import SimpleNamespace
import uuid

from app.models.claim import VerificationLevel
from app.pipeline.phase4_claims.verifier import (
    VERIFY_BATCH_CHAR_BUDGET,
    _apply_verification_ceiling,
    _build_verifier_claim_payload,
    _build_research_gaps,
    _estimate_verifier_payload_chars,
    _index_provenance_asset_types,
    _index_provenance_contexts,
    _is_meaningful_attribution,
    _rule_based_fallback,
    _split_verifier_batches,
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
        ]
    )

    assert [(gap["branch_id"], gap["question_id"]) for gap in gaps] == [("b1", "q1"), ("b2", "q2")]
    assert gaps[0]["question"] == "How does it work?"
    assert gaps[0]["intent"] == "explain"
    assert gaps[0]["found_assets"] == ["MECHANISM"]
    assert gaps[1]["found_assets"] == ["TIMELINE"]


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
    claim = SimpleNamespace(id=uuid.uuid4(), statement="Claim statement")
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
