from types import SimpleNamespace
import uuid

from app.pipeline.phase4_claims.claim_builder import _build_branch_batches


def test_build_branch_batches_uses_contextual_asset_types_per_branch() -> None:
    evidence_id = str(uuid.uuid4())
    evidence_index = {
        evidence_id: {
            "id": evidence_id,
            "verbatim_quote": "Banks issue notes against Certificates of Indebtedness.",
            "canonical_asset_type": "timeline",
            "narrative_value_score": 0.5,
            "reliability_hint": "high",
            "source_id": str(uuid.uuid4()),
            "source": {
                "title": "HKMA explainer",
                "url": "https://example.com",
                "type": "web",
                "domain_reliability": "high",
                "authority_type": "secondary",
                "is_temporary": False,
                "published_at": None,
            },
            "lineage": {
                "branch_ids": [],
                "question_ids": [],
                "retrieved_by": [],
            },
        }
    }
    provenances = [
        SimpleNamespace(
            evidence_id=uuid.UUID(evidence_id),
            branch_id="b3",
            question_id="b3.1",
            retrieved_by="hunter",
            asset_type="mechanism",
        ),
        SimpleNamespace(
            evidence_id=uuid.UUID(evidence_id),
            branch_id="b6",
            question_id="b6.2",
            retrieved_by="librarian_fast",
            asset_type="contrast",
        ),
    ]

    batches = dict(_build_branch_batches(evidence_index, provenances))

    assert batches["b3"][0]["asset_type"] == "mechanism"
    assert batches["b3"][0]["contextual_asset_types"] == ["mechanism"]
    assert batches["b3"][0]["canonical_asset_type"] == "timeline"

    assert batches["b6"][0]["asset_type"] == "contrast"
    assert batches["b6"][0]["contextual_asset_types"] == ["contrast"]
    assert batches["b6"][0]["lineage"]["branch_ids"] == ["b3", "b6"]
