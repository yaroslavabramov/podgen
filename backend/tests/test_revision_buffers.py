"""Source-bound separators and insertions do not require the model to encode join whitespace."""

from copy import deepcopy

import pytest

from podgen.domain.canonical import digest
from podgen.writing.revision.apply import materialize
from podgen.writing.revision.targets import target_catalog
from tests.revision_cases import reviews_for


@pytest.mark.parametrize(
    "quote,deleted,replaced",
    [
        ("First sentence.", "Second sentence. Third sentence.", "Changed. Second sentence. Third sentence."),
        ("Second sentence.", "First sentence. Third sentence.", "First sentence. Changed. Third sentence."),
        ("Third sentence.", "First sentence. Second sentence.", "First sentence. Second sentence. Changed."),
    ],
)
def test_first_middle_and_last_sentence_keep_the_correct_external_separator(quote, deleted, replaced):
    script = dict(utterances=[dict(speaker="A", text="First sentence. Second sentence. Third sentence.")])
    catalog = target_catalog(script, reviews_for(script, quote))
    target = next(t for t in catalog if t.get("quote") == quote)
    assert len([t for t in catalog if t.get("quote") == quote]) == 1
    for operation, text, expected in (("delete", None, deleted), ("replace", "Changed.", replaced)):
        edit = dict(operation=operation, target_id=target["target_id"])
        if text is not None:
            edit["text"] = text
        result, _, proof = materialize(
            "targeted",
            dict(edits=[edit], composition_update=None),
            script=script,
            catalog=catalog,
            speakers=["A"],
            composition=None,
            expected_hash=digest(script),
        )
        assert result["utterances"][0]["text"] == expected
        assert "  " not in expected
        effect = proof["edits"][0]
        assert script["utterances"][0]["text"][effect["start"] : effect["end"]] == effect["before"]


def test_a_number_has_a_supported_sentence_context_without_authorizing_another_sentence():
    script = dict(
        utterances=[dict(speaker="A", text="There are 83 registrations. Another statement remains.")]
    )
    catalog = target_catalog(script, reviews_for(script, "83"))
    assert {t["quote"] for t in catalog if t["kind"] == "text"} == {"83", "There are 83 registrations."}


def test_insert_at_first_last_and_repeated_boundary_and_delete_everything():
    script = dict(utterances=[dict(speaker="A", text="First."), dict(speaker="A", text="Last.")])
    reviews = reviews_for(script, "First.")
    reviews["fact"]["revision_scope"]["dependent_passages"] = [
        dict(reason="Dependent ending.", anchors=[dict(utterance_index=1, quote="Last.")])
    ]
    catalog = target_catalog(script, reviews)

    def run(edits):
        return materialize(
            "targeted",
            dict(edits=edits, composition_update=None),
            script=script,
            catalog=catalog,
            speakers=["A"],
            composition=None,
            expected_hash=digest(script),
        )[0]

    before = dict(operation="insert", target_id="B0", utterances=[dict(speaker="A", text="Before.")])
    after = dict(operation="insert", target_id="B2", utterances=[dict(speaker="A", text="After.")])
    assert [r["text"] for r in run([before, after])["utterances"]] == ["Before.", "First.", "Last.", "After."]
    with pytest.raises(ValueError, match="revision_insertions_overlap"):
        run([before, deepcopy(before)])
    with pytest.raises(ValueError):
        run([dict(operation="delete", target_id=t["target_id"]) for t in catalog if t["kind"] == "text"])


def test_source_separator_is_not_replaced_by_arbitrary_model_whitespace():
    script = dict(utterances=[dict(speaker="A", text="First. Last.")])
    catalog = target_catalog(script, reviews_for(script, "First."))
    target = next(t for t in catalog if t.get("quote") == "First.")
    with pytest.raises(ValueError, match="revision_duplicate_boundary_separator"):
        materialize(
            "targeted",
            dict(
                edits=[dict(operation="replace", target_id=target["target_id"], text="Changed. ")],
                composition_update=None,
            ),
            script=script,
            catalog=catalog,
            speakers=["A"],
            composition=None,
            expected_hash=digest(script),
        )
