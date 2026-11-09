"""Versioned delivery of editorial choices: exact saved inputs and an independent current preview."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.author_reply import contract_pin
from podgen.writing.model_references import CATALOG_KEY, bind_schema, preview_references
from podgen.writing.plan_selection import KEY, OPTIONS_VERSION, VERSION, from_manifest
from podgen.writing.projection_versions import V8, V9
from podgen.writing.reading_views import reading_payload
from podgen.writing.story_views import narrative_decisions

from tests.historical_preview import replay_preview as preview_request
from tests.revision_cases import composition, scope
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture

OPTIONS_GUIDANCE = (
    "Conditional extensions or cuts from the plan. Use them when adjusting the "
    "episode's scope; listing them does not mean every item should be spoken."
)

FIXTURES = Path(__file__).parent / "fixtures/plan-selection"


def snapshot(materials, profile, stage, contract, *, projection_version=V9):
    refs = preview_references(materials, projection_version)
    manifest = dict(
        role_projection_version=projection_version,
        response_schemas={
            stage: bind_schema(
                response_schema(stage, "editorial-role-packets/1", projection_version=projection_version),
                refs,
            )
        },
        **{CATALOG_KEY: refs.as_payload()},
    )
    if projection_version == V9:
        manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    if contract is not None:
        manifest[KEY] = contract
    return PreviewSnapshot(manifest=manifest, instructions="Exact pinned author instructions.")


def test_saved_snow_plan_is_unchanged_and_only_its_editorial_options_are_added():
    origin = json.loads((FIXTURES / "provenance.json").read_text())
    for file, key in [("snow-plan.json", "plan_sha256"), ("snow-author-input.json", "author_input_sha256")]:
        assert hashlib.sha256((FIXTURES / file).read_bytes()).hexdigest() == origin[key]
    plan = json.loads((FIXTURES / "snow-plan.json").read_text())
    author = json.loads((FIXTURES / "snow-author-input.json").read_text())
    before = deepcopy(plan)
    old = narrative_decisions(plan, projection_version=V9)
    assert old == author["story"]
    assert json.dumps(old, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        author["story"], ensure_ascii=False, separators=(",", ":")
    )
    new = narrative_decisions(plan, projection_version=V9, plan_selection_contract=VERSION)
    assert new == dict(
        old,
        material_options=dict(
            guidance=OPTIONS_GUIDANCE, choices=plan["plan"]["duration_strategy"]["optional_material"]
        ),
    )
    assert (
        "61" in new["material_options"]["choices"][0]
        and "расширение" in new["material_options"]["choices"][0]
    )
    assert "assumptions" not in new["duration_strategy"]
    assert new["acts"] == old["acts"]
    assert plan == before
    # A historical M4 inside free prose remains verbatim; no heuristic rewriting of archived choices.
    assert "M4" in new["material_options"]["choices"][0]


def test_independent_preview_keeps_the_selected_detail_and_optional_extension_separate_from_timing():
    materials, _, plan, profile, script, reader, fact, _ = fixture()
    plan = deepcopy(plan)
    for review in (reader, fact):
        review["revision_scope"] = scope(anchors=review["findings"][0]["anchors"])
    chosen = "Follow one return signal to show how the two clocks can be compared."
    option = "If there is room, develop a second exchange to show how the estimate changes."
    plan["plan"]["acts"][0]["intended_change_in_understanding"] = chosen
    plan["plan"]["duration_strategy"]["optional_material"] = [option]
    plan["plan"]["duration_strategy"]["assumptions"] = [
        "INTERNAL_TIMING_CALCULATION_NOT_AN_EDITORIAL_INSTRUCTION"
    ]
    initial = payload(preview_request(uuid4(), "draft", profile, materials, approved_plan=plan))
    assert initial["story"]["acts"][0]["intended_change_in_understanding"] == chosen
    assert initial["story"]["material_options"] == dict(guidance=OPTIONS_GUIDANCE, choices=[option])
    assert "INTERNAL_TIMING_CALCULATION" not in json.dumps(initial)
    revised = payload(
        preview_request(
            uuid4(),
            "revise-1",
            profile,
            materials,
            draft=script,
            reader_feedback=reader,
            fact_feedback=fact,
            approved_plan=plan,
        )
    )
    assert revised["editorial_frame"]["material_options"] == dict(guidance=OPTIONS_GUIDANCE, choices=[option])
    assert revised["editorial_frame"]["authority"] == "revisable_authorial_decisions"
    current = composition("Current")
    current["decisions"] = ["Develop one exchange and finish at the observed difference."]
    after = payload(
        preview_request(
            uuid4(),
            "revise-2",
            profile,
            materials,
            draft=script,
            reader_feedback=reader,
            fact_feedback=fact,
            approved_plan=plan,
            composition=current,
        )
    )
    without_recomposition = revised["editorial_frame"]["accepted_plan_scenes"]
    assert after["editorial_frame"]["accepted_plan_scenes"] == without_recomposition
    assert without_recomposition["acts"][0]["intended_change_in_understanding"] == chosen
    assert after["editorial_frame"]["decisions"] == current["decisions"]
    assert option not in json.dumps(after["editorial_frame"])
    assert "material_options" not in after["editorial_frame"]


def test_saved_v9_preview_without_the_new_pin_preserves_the_entire_old_request():
    materials, _, plan, profile, *_ = fixture()
    plan = deepcopy(plan)
    plan["plan"]["duration_strategy"]["optional_material"] = ["A concrete optional scene."]
    old = snapshot(materials, profile, "draft", None)
    new = snapshot(materials, profile, "draft", OPTIONS_VERSION)
    run_id = uuid4()
    baseline = preview_request(run_id, "draft", profile, materials, approved_plan=plan, snapshot=old)
    changed = preview_request(run_id, "draft", profile, materials, approved_plan=plan, snapshot=new)
    assert from_manifest(old.manifest) is None
    assert "material_options" not in payload(baseline)["story"]
    assert payload(changed)["story"] == dict(
        payload(baseline)["story"],
        material_options=dict(
            guidance=OPTIONS_GUIDANCE, choices=plan["plan"]["duration_strategy"]["optional_material"]
        ),
    )
    assert baseline["text"] == changed["text"]
    assert baseline["instructions"] == changed["instructions"]
    assert preview_request(run_id, "draft", profile, materials, approved_plan=plan, snapshot=old) == baseline
    for version in (None, V8, V9):
        projection = {} if version is None else dict(role_projection_version=version)
        assert from_manifest(projection) is None


@pytest.mark.parametrize(("projection_version", "contract"), [(V8, VERSION), (V9, "unknown")])
def test_preview_rejects_a_selection_contract_incompatible_with_the_saved_projection(
    projection_version, contract
):
    materials, _, plan, profile, *_ = fixture()
    corrupt = snapshot(materials, profile, "draft", contract, projection_version=projection_version)
    with pytest.raises(ContractError, match="unsupported_plan_selection_contract"):
        preview_request(uuid4(), "draft", profile, materials, approved_plan=plan, snapshot=corrupt)


def test_two_saved_compositions_supersede_the_original_material_options():
    from podgen.domain.canonical import digest
    from podgen.runtime.evaluation import script_revision

    materials, _, plan, _, script, reader, fact, _ = fixture()
    plan = deepcopy(plan)
    option = "An optional second trial from the original plan."
    plan["plan"]["duration_strategy"]["optional_material"] = [option]
    readings = dict(reader=reader, fact=fact)
    before = deepcopy(plan)
    source = deepcopy(script)
    carried = None
    for version, choice in enumerate(
        ("Develop only the first trial.", "Develop the result comparison without adding a second trial.")
    ):
        revised = deepcopy(source)
        revised["utterances"][0]["text"] += " Revised scene " + str(version) + "."
        current = composition(str(version))
        current["decisions"] = [choice]
        output, carried, _ = script_revision.apply(
            dict(mode="structural", script_version=version, targets=[], sequences=[]),
            dict(script=revised, composition=current),
            script=source,
            composition=carried,
            speakers=materials.speaker_labels,
            expected_hash=digest(source),
        )
        frame = reading_payload(
            "revise",
            materials,
            script=output,
            readings=readings,
            decision={"recommendation": "revise"},
            approved_plan=plan,
            composition=carried,
            projection_version=V9,
            plan_selection_contract=VERSION,
        )["editorial_frame"]
        assert frame["decisions"] == [choice]
        assert "material_options" not in frame
        assert option not in json.dumps(frame)
        source = output
    assert plan == before


@pytest.mark.parametrize("pin", [OPTIONS_VERSION, VERSION])
def test_cold_readings_remain_byte_identical_when_the_plan_handoff_is_enabled(pin):
    materials, _, _, _, script, *_ = fixture()
    for stage in ("reader-review", "fact-review"):
        old = reading_payload(stage, materials, script=script, projection_version=V9)
        new = reading_payload(
            stage, materials, script=script, projection_version=V9, plan_selection_contract=pin
        )
        assert json.dumps(new, ensure_ascii=False, separators=(",", ":")) == json.dumps(
            old, ensure_ascii=False, separators=(",", ":")
        )
        assert "material_options" not in new
