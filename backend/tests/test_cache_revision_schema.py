"""Stable physical schemas do not widen the server-bound permission for any edit kind."""

from copy import deepcopy
from uuid import uuid4

import pytest
from podgen.domain.canonical import digest
from podgen.runtime.evaluation import script_revision
from podgen.runtime.evaluation.compiler import response_request
from podgen.writing import prompt_cache
from podgen.writing.author_reply import wire_schema
from podgen.writing.projection_versions import V9

from tests.revision_cases import composition, reviews_for, scope
from tests.source_excerpt_cases import data_and_profile


def manifest(pinned):
    result = dict(
        script_revision_contract=script_revision.VERSION,
        revision_response_schemas=script_revision.schemas(
            prompt_cache_layout=prompt_cache.VERSION if pinned else None
        ),
    )
    if pinned:
        result.update(
            behavior_profile="production/1",
            role_projection_version=V9,
            **{prompt_cache.KEY: prompt_cache.VERSION},
        )
    return result


def selections(mode):
    script = dict(
        utterances=[
            dict(speaker="A", text=text) for text in ("Opening. Another sentence.", "Middle clue.", "Ending.")
        ]
    )
    first, second = reviews_for(script, "Opening."), reviews_for(script, "Ending.", 2)
    if mode == "sequence":
        for reviews, index in ((first, 0), (second, 1)):
            reviews["reader"].update(
                recommendation="revise",
                revision_scope=scope(
                    "sequence",
                    sequence_ranges=[
                        dict(
                            anchors=[
                                dict(utterance_index=index, quote=script["utterances"][index]["text"]),
                                dict(
                                    utterance_index=index + 1, quote=script["utterances"][index + 1]["text"]
                                ),
                            ],
                            reason="Reorder this scene.",
                        )
                    ],
                ),
            )
    if mode == "structural":
        for reviews in (first, second):
            reviews["reader"]["revision_scope"] = scope("whole_episode")
    return script, script_revision.operation(script, first, 0), script_revision.operation(script, second, 1)


@pytest.mark.parametrize("mode", ["targeted", "sequence", "structural"])
def test_two_different_target_sets_have_the_exact_same_output_format_with_the_new_pin(mode):
    script, first, second = selections(mode)
    assert first["mode"] == second["mode"] == mode
    assert first["targets"] != second["targets"] or mode == "structural"
    pinned = manifest(True)
    script_revision.validate_manifest(pinned)
    schemas = [script_revision.bound_schema(pinned, selection) for selection in (first, second)]
    assert schemas[0] == schemas[1]
    _, _, _, _, _, _, _, _, profile = data_and_profile()
    bodies = [
        response_request(
            uuid4(),
            "revise-" + str(index),
            profile("revise-" + str(index)),
            dict(revision_operation=script_revision.model_operation(selection)),
            "Same revision instructions",
            wire_schema(schema, speaker_labels=["A"]),
            prompt_cache_layout=prompt_cache.VERSION,
        )
        for index, (selection, schema) in enumerate(zip((first, second), schemas, strict=True), 1)
    ]
    assert bodies[0]["text"]["format"] == bodies[1]["text"]["format"]
    if mode != "structural":
        assert script_revision.bound_schema(manifest(False), first) != script_revision.bound_schema(
            manifest(False), second
        )
        assert "enum" not in schemas[0]["$defs"]["ReplaceText"]["properties"]["target_id"]
        assert schemas[0]["$defs"]["ReplaceText"]["properties"]["target_id"]["pattern"] == r"^T[1-9][0-9]*$"


@pytest.mark.parametrize("operation,target", [("replace", "T999"), ("delete", "T999"), ("insert", "B999")])
def test_outside_selection_ids_are_rejected_before_any_script_can_be_saved(operation, target):
    script, selection, _ = selections("targeted")
    source = deepcopy(script)
    edit = dict(operation=operation, target_id=target)
    if operation == "replace":
        edit["text"] = "Unsupported change."
    elif operation == "insert":
        edit["utterances"] = [dict(speaker="A", text="Unsupported insertion.")]
    with pytest.raises(ValueError, match="revision_target_not_offered"):
        script_revision.apply(
            selection,
            dict(edits=[edit], composition_update=None),
            script=script,
            composition=None,
            speakers=["A"],
            expected_hash=digest(script),
        )
    assert script == source


def test_unknown_sequence_id_is_rejected_and_unchanged_historical_enums_are_still_bound():
    script, selection, _ = selections("sequence")
    source = deepcopy(script)
    with pytest.raises(ValueError, match="revision_sequence_not_offered"):
        script_revision.apply(
            selection,
            dict(
                sequences=[
                    dict(range_id="R999", utterances=[dict(speaker="A", text="Unsupported sequence.")])
                ],
                edits=[],
                composition=composition(),
            ),
            script=script,
            composition=None,
            speakers=["A"],
            expected_hash=digest(script),
        )
    assert script == source
    old = manifest(False)
    script_revision.validate_manifest(old)
    schema = script_revision.bound_schema(old, selection)
    assert schema["$defs"]["SequenceEdit"]["properties"]["range_id"]["enum"] == [
        r["range_id"] for r in selection["sequences"]
    ]
    assert "pattern" not in schema["$defs"]["SequenceEdit"]["properties"]["range_id"]
