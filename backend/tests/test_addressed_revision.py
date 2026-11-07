"""Real 83 corrections retain the entire untouched script and explicit editorial scale."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.runtime.evaluation import script_revision
from podgen.writing.revision.apply import materialize
from podgen.writing.revision.contracts import TargetedRevision
from podgen.writing.revision.targets import target_catalog
from tests.revision_cases import actual_case, corrections_83, reviews_for


def test_complete_archived_request_matches_the_actual_83_source():
    raw = (Path(__file__).parent / "fixtures/revision-83.request.json").read_bytes()
    assert sha256(raw).hexdigest() == "8379af6318c23a3a371b67f6a35d9aff0346a4b652e52d3936e8cd30f0734707"


def test_actual_three_fact_repairs_are_targeted_and_every_other_character_is_retained():
    _, _, script, reviews = actual_case()
    assert reviews["reader"]["recommendation"] == "keep"
    assert reviews["fact"]["revision_scope"]["scale"] == "local"
    original = deepcopy(script)
    selection = script_revision.operation(script, reviews, 0)
    assert selection["mode"] == "targeted"
    edits, expected = [], deepcopy(script)
    for anchor in corrections_83():
        text = anchor["text"]
        target = next(
            t
            for t in selection["targets"]
            if t.get("utterance_index") == anchor["utterance_index"] and t.get("quote") == anchor["quote"]
        )
        edits.append(dict(operation="replace", target_id=target["target_id"], text=text))
        row = expected["utterances"][anchor["utterance_index"]]
        row["text"] = row["text"].replace(anchor["quote"], text, 1)
    wire = dict(edits=edits, composition_update=None)
    output, state, proof = script_revision.apply(
        selection,
        wire,
        script=script,
        composition=None,
        speakers={r["speaker"] for r in script["utterances"]},
        expected_hash=digest(script),
    )
    assert output == expected and script == original and state is None
    assert "дня. У каждого" in output["utterances"][0]["text"]
    assert proof["application"]["unchanged_outside_targets"]
    view = script_revision.model_operation(selection)
    assert (
        len(
            [
                t
                for t in view["targets"]
                if t.get("text") == reviews["fact"]["findings"][0]["anchors"][0]["quote"]
            ]
        )
        == 1
    )
    for target in view["targets"]:
        assert not {"start", "end", "basis", "speaker", "delete"} & set(target)


def test_keep_scope_praise_never_authorizes_a_fact_only_change_but_optional_findings_do():
    script = dict(
        utterances=[dict(speaker="A", text="A sound opening."), dict(speaker="A", text="A false number.")]
    )
    reviews = reviews_for(script, "A false number.", 1)
    reviews["reader"]["revision_scope"]["anchors"] = [dict(utterance_index=0, quote="A sound opening.")]
    assert all(t.get("utterance_index") != 0 for t in target_catalog(script, reviews))
    reviews["reader"]["findings"] = [
        dict(priority="optional", anchors=[dict(utterance_index=0, quote="A sound opening.")])
    ]
    assert any(t.get("utterance_index") == 0 for t in target_catalog(script, reviews))


def test_repeated_quote_does_not_silently_authorize_both_occurrences():
    script = dict(utterances=[dict(speaker="A", text="Same word. Same word.")])
    with pytest.raises(ValueError, match="revision_target_quote_ambiguous"):
        target_catalog(script, reviews_for(script, "Same word."))


@pytest.mark.parametrize("editor", ["reader", "fact"])
def test_one_editor_can_choose_whole_episode_without_a_count_heuristic(editor):
    _, _, script, reviews = actual_case()
    reviews[editor]["recommendation"] = "revise"
    reviews[editor]["revision_scope"]["scale"] = "whole_episode"
    if editor == "fact":
        reviews["fact"]["findings"] = reviews["fact"]["findings"][:1]
    assert script_revision.operation(script, reviews, 0)["mode"] == "structural"


def test_unknown_targets_overlap_noop_stale_and_empty_output_fail_without_mutating_source():
    script = dict(utterances=[dict(speaker="A", text="First sentence. Second sentence.")])
    original = deepcopy(script)
    selection = script_revision.operation(script, reviews_for(script, "First sentence."), 0)
    target = next(t for t in selection["targets"] if t.get("quote") == "First sentence.")

    def apply(edits, expected_hash=None):
        return materialize(
            "targeted",
            dict(edits=edits, composition_update=None),
            script=script,
            catalog=selection["targets"],
            speakers=["A"],
            composition=None,
            expected_hash=expected_hash or digest(script),
        )

    with pytest.raises(ValueError, match="revision_target_not_offered"):
        apply([dict(operation="delete", target_id="T999")])
    op = dict(operation="replace", target_id=target["target_id"], text="Changed.")
    with pytest.raises(ValueError, match="revision_text_edits_overlap"):
        apply([op, op])
    with pytest.raises(ValueError, match="revision_edit_is_noop"):
        apply([dict(op, text=target["quote"])])
    with pytest.raises(ValueError, match="revision_source_version_mismatch"):
        apply([op], "0" * 64)
    with pytest.raises(ValidationError):
        TargetedRevision.model_validate(dict(edits=[], composition_update=None))
    assert script == original
