from types import SimpleNamespace
import uuid

from app.pipeline.phase5_planning.planning_context import summarize_planning_context


def test_summarize_planning_context_surfaces_illustrative_and_intent_signals() -> None:
    claim_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    source_id = uuid.uuid4()
    claim_index, evidence_index = summarize_planning_context(
        claims=[
            SimpleNamespace(
                id=claim_id,
                claim_type="fact",
                verification_level="verified",
                statement="Banks issue notes against Certificates of Indebtedness.",
            )
        ],
        claim_rows=[
            (
                SimpleNamespace(
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    supports_fact=True,
                    contradicts_fact=False,
                    illustrative=True,
                ),
                SimpleNamespace(
                    id=evidence_id,
                    asset_type="mechanism",
                    verbatim_quote="Banks issue notes against Certificates of Indebtedness.",
                    narrative_value_score=0.7,
                ),
                SimpleNamespace(
                    id=source_id,
                    is_duplicate_of=None,
                    title="HKMA explainer",
                    domain_reliability="high",
                    authority_type="primary",
                    is_temporary=False,
                ),
            )
        ],
        provenances=[
            SimpleNamespace(
                evidence_id=evidence_id,
                branch_id="b3",
                question_id="b3.1",
                asset_type="mechanism",
                direct_or_contextual="direct",
            )
        ],
        question_context_map={
            "b3.1": {"question": "How does issuance work?", "intent": "explain"},
        },
    )

    claim_summary = claim_index[str(claim_id)]
    evidence_summary = evidence_index[str(evidence_id)]

    assert claim_summary["has_illustrative"] is True
    assert claim_summary["illustrative_evidence_count"] == 1
    assert claim_summary["direct_support_count"] == 1
    assert claim_summary["intents"] == ["explain"]
    assert claim_summary["source_diversity"] == 1
    assert claim_summary["quality_hints"]["has_primary_support"] is True
    assert evidence_summary["illustrative"] is True
    assert evidence_summary["direct_or_contextual"] == ["direct"]
    assert evidence_summary["question_contexts"][0]["intent"] == "explain"
