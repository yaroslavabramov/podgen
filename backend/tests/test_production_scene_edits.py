"""A scene commissioned by the reader survives the real author wire and edit application."""

import json
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from podgen.domain.canonical import digest
from podgen.runtime.evaluation import script_revision
from podgen.runtime.evaluation.preview import preview_request
from podgen.writing.behavior import resolve_behavior
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.reader_schema import feedback_model
from podgen.writing.roles import WriteOutput

from tests.revision_cases import composition, scope
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture


@pytest.mark.parametrize("format", ["solo", "dialogue"])
@pytest.mark.parametrize("single_utterance", [False, True])
def test_complete_scene_can_be_cut_reordered_and_replayed_through_the_author_wire(format, single_utterance):
    materials, _, _, profile, _, reader, fact, _ = fixture()
    packet = materials.packet.model_dump(mode="json")
    packet["assignment"]["format"] = format
    packet["assignment"]["speakers"] = [dict(packet["assignment"]["speakers"][0], label="A")]
    if format == "dialogue":
        packet["assignment"]["speakers"].append(dict(packet["assignment"]["speakers"][0], label="B"))
    materials = replace(materials, packet=EditorialInput.model_validate(packet))
    last_speaker = materials.speaker_labels[-1]
    scene = [
        dict(speaker="A", text="The result arrives. A dispensable aside."),
        dict(speaker=last_speaker, text="The route to the result."),
        dict(speaker="A", text="The observation that starts the route."),
    ]
    if single_utterance:
        scene = [dict(speaker="A", text=" ".join(row["text"] for row in scene))]
    script = dict(
        utterances=[
            dict(speaker="A", text="Protected opening."),
            *scene,
            dict(speaker=last_speaker, text="Protected ending."),
        ]
    )
    first = dict(utterance_index=1, quote="The result arrives.")
    last = dict(utterance_index=len(scene), quote="The observation")
    reader["findings"][0].update(
        anchors=[first],
        dimension="structure",
        observation="The result spends the discovery early.",
        requested_change="Develop the observation first, then the route and its result; cut the aside.",
    )
    reader["revision_scope"] = scope(
        "sequence",
        anchors=[first],
        sequence_ranges=[dict(anchors=[first, last], reason="Rebuild this scene.")],
    )
    fact.update(recommendation="keep", findings=[], revision_scope=scope("none"))
    profile = copy(profile)
    profile.model_revision_or_alias = "gpt-6.1-terra"
    profile.output_limit, profile.context_limit = 24576, 200000
    profile.generation_parameters = dict(
        store=False, background=False, reasoning=dict(effort="medium"), text=dict(verbosity="high")
    )
    behavior = resolve_behavior("production/1")
    reader_request = preview_request(
        uuid4(), "reader-review", profile, materials, behavior_profile="production/1", draft=script
    )
    reader_schema = reader_request["text"]["format"]["schema"]
    archive = Path(__file__).resolve().parents[2] / (
        "evals/reports/2026-11-08-snow-narrative-rebuild/calls/20-reader-review/podgen-request.json"
    )
    historical_schema = json.loads(archive.read_text())["text"]["format"]["schema"]
    assert json.dumps(reader_schema) == json.dumps(historical_schema)
    Draft202012Validator(reader_schema).validate(reader)
    feedback_model(behavior.reader_schema).model_validate(reader).validate_against(
        WriteOutput.model_validate(script)
    )
    profile.model_revision_or_alias = "claude-opus-5-5"
    profile.provider = "anthropic"
    profile.api_family = "messages"
    profile.adapter_version = "anthropic-messages-v1"
    request = preview_request(
        uuid4(),
        "revise-1",
        profile,
        materials,
        behavior_profile="production/1",
        draft=script,
        reader_feedback=reader,
        fact_feedback=fact,
    )
    visible = payload(request)
    assert visible["revision_operation"]["mode"] == "sequence"
    assert visible["revision_operation"]["sequences"] == [
        dict(
            range_id="R1",
            first_utterance_index=1,
            last_utterance_index=len(scene),
        )
    ]
    assert [row["text"] for row in visible["script"]["utterances"]] == [
        row["text"] for row in script["utterances"]
    ]
    replacement = [
        dict(speaker="A", text="The observation that starts the route."),
        dict(speaker=last_speaker, text="The route to the result."),
        dict(speaker="A", text="The result arrives."),
    ]
    wire = dict(sequences=[dict(range_id="R1", utterances=replacement)], edits=[], composition=composition())
    Draft202012Validator(request["text"]["format"]["schema"]).validate(dict(result=wire))
    readings = dict(reader=reader, fact=fact)
    selection = script_revision.operation(script, readings, 0)
    before = deepcopy(script)
    output, state, proof = script_revision.apply(
        selection,
        wire,
        script=script,
        composition=None,
        speakers=materials.speaker_labels,
        expected_hash=digest(script),
    )
    assert script == before
    assert output["utterances"] == [script["utterances"][0], *replacement, script["utterances"][-1]]
    assert proof["application"]["sequences"] == [dict(start=1, end=len(scene) + 1)]
    script_revision.verify_record(
        dict(revision=proof, output=output, composition=state),
        script=script,
        composition=None,
        readings=readings,
        speakers=materials.speaker_labels,
        version=0,
    )
