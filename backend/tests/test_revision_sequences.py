"""A bounded sequence is not permission to rewrite the protected opening or other passages."""

from copy import deepcopy

import pytest

from podgen.domain.canonical import digest
from podgen.runtime.evaluation import script_revision
from tests.revision_cases import composition, reviews_for, scope


def test_explicit_middle_sequence_preserves_the_opening_and_all_other_text():
    script = dict(
        utterances=[
            dict(speaker="A", text=t)
            for t in ("Good opening.", "First clue.", "Second clue.", "Good ending.")
        ]
    )
    readings = reviews_for(script, "First clue.", 1)
    readings["reader"].update(
        recommendation="revise",
        revision_scope=scope(
            "sequence",
            anchors=[dict(utterance_index=0, quote="Good opening.")],
            sequence_ranges=[
                dict(
                    anchors=[
                        dict(utterance_index=1, quote="First clue."),
                        dict(utterance_index=2, quote="Second clue."),
                    ],
                    reason="Reverse the discovery order.",
                )
            ],
        ),
    )
    selection = script_revision.operation(script, readings, 0)
    assert selection["mode"] == "sequence"
    assert [(r["start"], r["end"]) for r in selection["sequences"]] == [(1, 3)]
    assert not any(t.get("utterance_index") == 0 for t in selection["targets"])
    wire = dict(
        sequences=[dict(range_id="R1", utterances=list(reversed(script["utterances"][1:3])))],
        edits=[],
        composition=composition(),
    )
    output, state, proof = script_revision.apply(
        selection,
        wire,
        script=script,
        composition=None,
        speakers=["A"],
        expected_hash=digest(script),
        unresolved_questions=[dict(question="Still open?", why_needed="Order alone does not answer it.")],
    )
    assert output["utterances"] == [
        script["utterances"][0],
        script["utterances"][2],
        script["utterances"][1],
        script["utterances"][3],
    ]
    assert state["unresolved_questions"][0]["question"] == "Still open?"
    assert proof["application"]["unchanged_outside_targets"]
    bad = deepcopy(wire)
    target = next(t for t in selection["targets"] if t.get("quote") == "First clue.")
    bad["edits"] = [dict(operation="delete", target_id=target["target_id"])]
    with pytest.raises(ValueError, match="revision_edits_overlap_sequence"):
        script_revision.apply(
            selection, bad, script=script, composition=None, speakers=["A"], expected_hash=digest(script)
        )


def test_legacy_sequence_without_explicit_boundaries_is_not_reported_as_bounded():
    script = dict(utterances=[dict(speaker="A", text="A clue.")])
    readings = reviews_for(script, "A clue.")
    readings["reader"].update(recommendation="revise", revision_scope=scope("sequence"))
    assert script_revision.operation(script, readings, 0)["mode"] == "structural"
