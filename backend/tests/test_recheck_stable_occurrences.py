"""Opt-in occurrence identity never guesses which identical turn was removed."""

from copy import deepcopy

import pytest

from podgen.writing.reading_audit.recheck_changes import attach, comparison


def script(*texts):
    return {"utterances": [{"speaker": "HOST", "text": text} for text in texts]}


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (("A", "B", "A"), ("A", "B", "A"), {0: 0, 1: 1, 2: 2}),
        (("A", "A"), ("intro", "A", "A", "outro"), {0: 1, 1: 2}),
        (("A", "A"), ("A", "new", "A"), {0: 0, 1: 2}),
        (("intro", "A", "A", "outro"), ("A", "A"), {1: 0, 2: 1}),
        (("A", "A"), ("A",), {}),
        (("A",), ("A", "A"), {}),
        # No unique landmark separates these groups: conservatively recheck both.
        (("A", "A", "B", "B"), ("A", "A", "B"), {}),
        (("A", "A", "B"), ("A", "B"), {2: 1}),
        (("A", "B", "A"), ("A", "A", "B", "A"), {1: 2, 2: 3}),
        (("A", "B", "A"), ("A", "B", "A", "A"), {0: 0, 1: 1}),
        (("A", "B", "A"), ("B", "A", "A", "B"), {}),
        ((), ("A",), {}),
        (("A",), (), {}),
    ],
)
def test_unchanged_landmark_gaps_align_and_uncertain_gaps_remain_unresolved(old, new, expected):
    before, after = script(*old), script(*new)
    original = deepcopy((before, after))
    changes, alignment = comparison(before, after, stable_occurrences=True)
    assert alignment == expected
    assert changes == comparison(before, after)[0]
    assert (before, after) == original


def test_default_alignment_remains_the_archived_behavior():
    before = script("A", "B", "A")
    assert comparison(before, before) == ([], {1: 1})
    assert comparison(before, before, stable_occurrences=False) == ([], {1: 1})


def test_speaker_is_part_of_occurrence_identity():
    before = script("A", "A", "A")
    before["utterances"][1]["speaker"] = "GUEST"
    after = deepcopy(before)
    after["utterances"].pop(1)
    assert comparison(before, after, stable_occurrences=True)[1] == {0: 0, 2: 1}


def test_attach_keeps_each_decision_and_assignment_on_its_own_occurrence():
    before, after = script("A", "middle", "A"), script("new", "A", "middle", "A")
    anchors = [{"utterance_index": index, "quote": "A"} for index in (0, 2)]
    original = {
        "previous_decisions": [{"anchors": [anchor]} for anchor in anchors],
        "assignments": [{"anchors": deepcopy(anchors)}],
    }
    legacy = attach(deepcopy(original), before, after)
    assert all(row["requires_update"] for row in legacy["previous_decisions"])
    view = attach(deepcopy(original), before, after, stable_occurrences=True)
    for row, old_index, new_index in zip(view["previous_decisions"], (0, 2), (1, 3), strict=True):
        assert row["anchors"] == [{"utterance_index": new_index, "quote": "A"}]
        assert row["previous_utterance_indices"] == [old_index]
        assert row["anchor_version"] == "current"
        assert row["requires_update"] is False
    task = view["assignments"][0]
    assert task["anchors"] == anchors
    assert task["current_utterance_indices"] == [1, 3]


def test_attach_does_not_confirm_an_ambiguous_deleted_occurrence():
    view = {
        "previous_decisions": [{"anchors": [{"utterance_index": 0, "quote": "A"}]}],
        "assignments": [],
    }
    attach(view, script("A", "A"), script("A"), stable_occurrences=True)
    assert view["previous_decisions"][0]["requires_update"] is True
    assert view["previous_decisions"][0]["anchor_version"] == "previous"


def test_identical_script_with_repeated_turns_needs_no_reassessment():
    before = script("A", "middle", "A")
    view = {
        "previous_decisions": [{"anchors": [{"utterance_index": index, "quote": "A"}]} for index in (0, 2)],
        "assignments": [],
    }
    attach(view, before, deepcopy(before), stable_occurrences=True)
    assert view["script_changes"] == []
    assert all(row["requires_update"] is False for row in view["previous_decisions"])
    assert [row["anchors"][0]["utterance_index"] for row in view["previous_decisions"]] == [0, 2]


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (
            ("old intro", "L", "A", "A", "R", "old ending"),
            ("new intro", "inserted", "L", "A", "A", "R", "new ending"),
            {1: 2, 2: 3, 3: 4, 4: 5},
        ),
        (
            ("L", "A", "A", "M", "B", "B", "R"),
            ("L", "A", "A", "M", "B", "R"),
            {0: 0, 1: 1, 2: 2, 3: 3, 6: 5},
        ),
        # A still exists across the landmark: it cannot be filtered as removed.
        (("L", "A", "A", "M", "R"), ("L", "A", "M", "A", "R"), {0: 0, 3: 2, 4: 4}),
        # Moving unique landmarks must not attach a duplicate to a different scene.
        (
            ("L", "A", "A", "M", "B", "B", "R"),
            ("M", "A", "A", "L", "B", "B", "R"),
            {0: 3, 3: 0, 6: 6},
        ),
        # Even with ordered landmarks, moving a block changes both adjacent gaps.
        (
            ("L", "A", "A", "M", "B", "B", "R"),
            ("L", "B", "B", "M", "A", "A", "R"),
            {0: 0, 3: 3, 6: 6},
        ),
        (("A", "M", "A"), ("M", "A", "A"), {1: 0}),
    ],
)
def test_external_edits_preserve_only_the_matching_gaps(old, new, expected):
    assert comparison(script(*old), script(*new), stable_occurrences=True)[1] == expected


def test_unique_turn_permutation_keeps_the_existing_alignment():
    # Reordering unique turns retains the existing alignment, without examining
    # any gaps. A large input must not allocate a pairwise matching table.
    before = script(*(str(i) for i in range(1000)))
    after = script(*(str(i) for i in range(999, -1, -1)))
    assert comparison(before, after, stable_occurrences=True)[1] == {i: 999 - i for i in range(1000)}
