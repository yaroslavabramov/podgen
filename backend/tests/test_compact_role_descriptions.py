"""Actual schemas remain address-compatible; only new role pins select compact field semantics."""

import hashlib
import json
from copy import deepcopy

import pytest

from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation import script_revision
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing import ending_guidance
from podgen.writing.behavior import resolve_behavior
from podgen.writing.compact_editorial_descriptions import READER_FIELDS, READER_FINDING_FIELDS
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.reader_schema import COMPACT_VERSION, SCENE_COMPARISON_VERSION, feedback_model
from tests.revision_cases import scope
from tests.schema_cases import without_descriptions
from tests.source_excerpt_cases import data_and_profile, release
from tests.test_cache_revision_schema import manifest as revision_manifest
from tests.test_cache_revision_schema import selections
from tests.test_planning_v9 import selected
from tests.writing_instruction_history import historical_instructions

OLD = dict(
    plan="933d42194b42f45af89c54c294fd0d66f1c70001eeea33513895de2bf519a265",
    **{
        "plan-revise": "55f6064cf4ab5dd8359562c0aca328d94dd44911cbccdb530676b1f28173c408",
        "plan-review": "f65d60d0bd4cd5c29e2e61d357ea5bbe25369eeed5a4967929f8ab077f772375",
        "reader-review": "5fa00d503635ada0f568e75726a40e308a8616480d7bd4503e2f2f5624d80002",
        "draft": "13b21c996662ca29534c6ed2b0c2ac1ffd6ca324b25c1390be95e908c939059a",
        "dual-revise": "31bcb5fcd4c5443a23340af4a0653a5a1b922d70d3662e37801465892f10bfdf",
    },
)


def test_reader_has_only_compact_contents_and_keeps_shape_order_and_other_roles():
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        old = response_schema(
            stage, "editorial-role-packets/1", reader_schema_version=SCENE_COMPARISON_VERSION
        )
        current = response_schema(stage, "editorial-role-packets/1", reader_schema_version=COMPACT_VERSION)
        assert json.dumps(without_descriptions(current)) == json.dumps(without_descriptions(old))
        assert all(current["properties"][k]["description"] == v for k, v in READER_FIELDS.items())
        assert all(
            current["$defs"]["EditorialFinding"]["properties"][k]["description"] == v
            for k, v in READER_FINDING_FIELDS.items()
        )
        assert "repetition of the main point" not in json.dumps(current)
        assert "Repetition alone" not in json.dumps(current)
    assert feedback_model(COMPACT_VERSION) is feedback_model(SCENE_COMPARISON_VERSION)
    for stage in ("draft", "revise", "fact-review"):
        assert response_schema(
            stage,
            "editorial-role-packets/1",
            reader_schema_version=COMPACT_VERSION,
            planning_ending_guidance=ending_guidance.VERSION,
        ) == response_schema(
            stage,
            "editorial-role-packets/1",
            reader_schema_version=SCENE_COMPARISON_VERSION,
            planning_ending_guidance=ending_guidance.CONTEXT_VERSION,
        )


@pytest.mark.parametrize("cache", [None, "stable-role-prefix/1"])
def test_all_raw_and_dynamic_rewrite_schemas_keep_published_bytes(cache):
    expected = (
        {
            "targeted": "dfb29b87db9ef6cdfe2c0e389e77c617a814875f907baea2e647f2604d728b53",
            "sequence": "633334b42367b156a67a650f63f301edb4a7a71b7d960ac4038dbedee9db7780",
            "structural": "fa00e6f176a874b159b26eb942213d6cb726c51365906e5fe0a843feae81871a",
        }
        if cache is None
        else {
            "targeted": "64b9e047027cefaa5e5f21fe173fb0e836bc2061729969058e730d7e369685ea",
            "sequence": "861eaabc98eca3a69bd25186ef1f22f68694816897555de5264e278eef7fbcf9",
            "structural": "fa00e6f176a874b159b26eb942213d6cb726c51365906e5fe0a843feae81871a",
        }
    )
    raw = script_revision.schemas(prompt_cache_layout=cache)
    assert {
        key: hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        for key, value in raw.items()
    } == expected
    for mode in raw:
        _, selection, _ = selections(mode)
        old = revision_manifest(cache is not None)
        new = dict(
            old,
            planning_ending_guidance=ending_guidance.VERSION,
            reader_review_schema_version=COMPACT_VERSION,
        )
        assert script_revision.bound_schema(old, selection) == script_revision.bound_schema(new, selection)


@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_actual_five_role_previews_and_frozen_replay_for_both_formats(monkeypatch, format):
    materials, source, _, _, script, reader, fact, review, profile = data_and_profile()
    candidate = selected(source)
    if format == "dialogue":
        raw = materials.packet.model_dump(mode="json")
        raw["assignment"]["format"] = format
        raw["assignment"]["speakers"].append(
            dict(
                label="СОБЕСЕДНИК", function="Ask about the observations.", speech_register="Conversational."
            )
        )
        materials = EditorialMaterials(EditorialInput.model_validate(raw), materials.shared_ref, ())
        host = deepcopy(candidate["plan"]["host_functions"][0])
        host["speaker_id"] = "СОБЕСЕДНИК"
        candidate["plan"]["host_functions"].append(host)
        script["utterances"][-1]["speaker"] = "СОБЕСЕДНИК"
    _, _, command, current = release(monkeypatch, materials, profile)
    current.update(
        script_revision_contract=script_revision.VERSION,
        revision_response_schemas=script_revision.schemas(
            prompt_cache_layout=current.get("prompt_cache_layout")
        ),
    )
    old = deepcopy(current)
    old.update(
        planning_ending_guidance=ending_guidance.CONTEXT_VERSION,
        reader_review_schema_version=SCENE_COMPARISON_VERSION,
    )
    references = run_references(materials, current)
    anchor = dict(utterance_index=0, quote="First observation.")
    reader.update(revision_scope=scope("local", anchors=[anchor]), recommendation="revise")
    fact.update(revision_scope=scope("local", anchors=[anchor]), recommendation="revise")
    inputs = {
        "plan": {},
        "plan-revise": dict(candidate=candidate, plan_review=review),
        "plan-review": dict(candidate=candidate),
        "draft": dict(approved_plan=candidate),
        "reader-review": dict(draft=script),
        "revise-1": dict(draft=script, approved_plan=candidate, reader_feedback=reader, fact_feedback=fact),
    }
    for stage, kwargs in inputs.items():
        role = "dual-revise" if stage == "revise-1" else stage
        instructions = resolve_behavior().instructions(role, format).body
        identity = OLD[role]
        if format == "dialogue":
            identity = {
                "plan-review": "d79bad884366548c474ac500fa739f61b0c92b6e33e2ca28dec8fb2947186f09",
                "draft": "cf8863c91cf3139c8d5e51317240ea3417f98dcd807ce7a8d78686048ae1dd5a",
            }.get(role, identity)
        previous = historical_instructions(identity)
        assert hashlib.sha256(previous.encode()).hexdigest() == identity
        old["response_schemas"][stage] = bind_schema(
            response_schema(
                stage,
                command.input_contract,
                planning_ending_guidance=ending_guidance.CONTEXT_VERSION,
                reader_schema_version=SCENE_COMPARISON_VERSION,
                planning_output_contract=current.get("planning_output_contract"),
                plan_review_schema_version=current.get("plan_review_schema_version"),
                fact_schema_version=current.get("fact_review_schema_version"),
            ),
            references,
        )
        before = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(old, previous),
            **kwargs,
        )
        after = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(current, instructions),
            **kwargs,
        )
        assert before["instructions"] == previous and after["instructions"] == instructions
        if stage in ("draft", "revise-1"):
            assert after["text"]["format"]["schema"] == before["text"]["format"]["schema"]
        after["instructions"] = previous
        after["text"]["format"]["schema"] = before["text"]["format"]["schema"]
        assert before == after
