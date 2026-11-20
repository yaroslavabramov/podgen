import pytest
from scopes import Partition, handoff, validate_dispositions


def test_partition_requires_complete_contiguous_coverage():
    def ranges(*ends):
        plan = Partition(sections=[dict(subject="scene", last_utterance_index=i) for i in ends])
        return list(plan.ranges(4))

    assert ranges(0, 3) == [(0, 1), (1, 4)]
    for ends in [(2, 1), (0, 2), (0, 4), (1, 1)]:
        with pytest.raises(ValueError):
            ranges(*ends)


def test_local_aliases_and_quotes_keep_their_own_provenance():
    def record(source, verdict):
        evidence = dict(source_selectors=[dict(key=source, start=0, end=5)])
        anchors = [dict(utterance_index=0, quote=source)]
        return dict(
            reading_audit=dict(resolved_evidence={"current:E1": evidence}),
            output=dict(
                audit=dict(
                    claim_checks=[
                        dict(
                            decision_id="D1",
                            anchors=anchors,
                            contextual_meaning=source,
                            verdict=verdict,
                            source_refs=[dict(from_review="current", evidence_id="E1")],
                        )
                    ]
                ),
                findings=[],
                research_requests=[],
                important_limits=[],
            ),
        )

    sections, proofs, open_items = handoff(
        [(0, record("first", "supported")), (10, record("second", "unchecked"))]
    )
    assert sections[0]["decisions"][0]["sources"] == ["H1"]
    assert sections[1]["decisions"][0]["sources"] == ["H2"]
    assert proofs["H2"]["source_selectors"][0]["key"] == "second"
    assert open_items["U11D1"] == [dict(utterance_index=10, quote="second")]


def test_synthesis_cannot_drop_or_substitute_an_issue():
    quote = dict(utterance_index=8, quote="Sacy")
    issues = {"U8F1": [quote]}
    decision = dict(decision_id="D2", verdict="supported", anchors=[quote], source_refs=["H1"])
    output = dict(
        fragment_dispositions=[], audit=dict(claim_checks=[decision]), finding_links=[], recommendation="keep"
    )
    with pytest.raises(ValueError, match="missing_or_duplicate"):
        validate_dispositions(output, issues)
    output["fragment_dispositions"] = [
        dict(issue_id="U8F1", outcome="resolved", decision_id="D2", reason="source addresses it")
    ]
    validate_dispositions(output, issues)
    decision["anchors"] = [dict(utterance_index=7, quote="different")]
    with pytest.raises(ValueError, match="unrelated"):
        validate_dispositions(output, issues)
