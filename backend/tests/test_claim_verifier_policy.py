from types import SimpleNamespace
import uuid

from app.models.claim import VerificationLevel
from app.pipeline.phase4_claims.verifier import (
    _apply_verification_ceiling,
    _build_research_gaps,
    _index_provenance_asset_types,
    _is_meaningful_attribution,
    _rule_based_fallback,
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
                            "found_assets": ["MECHANISM"],
                        },
                        {
                            "branch_id": "b2",
                            "question_id": "q2",
                            "found_assets": ["TIMELINE"],
                        },
                    ],
                    "found_assets": ["MECHANISM", "TIMELINE"],
                },
            )
        ]
    )

    assert [(gap["branch_id"], gap["question_id"]) for gap in gaps] == [("b1", "q1"), ("b2", "q2")]
    assert gaps[0]["found_assets"] == ["MECHANISM"]
    assert gaps[1]["found_assets"] == ["TIMELINE"]
