"""Versioned writer route, cold duration-only context, and unabridged revision feedback."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.duration_views import duration_context
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences
from podgen.writing.projection_versions import V6, V7, V8
from podgen.writing.projections import actionable_feedback, role_payload
from podgen.writing.reading_views import reading_payload
from tests.test_editorial_independence import payload
from tests.test_role_semantics import ARCHIVE, archived_materials
from tests.test_v5_role_wire import fixture

ROOT = Path(__file__).resolve().parents[2]


def wire_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def historical_views(version):
    def read(name):
        return json.loads((ARCHIVE / name).read_text())["output"]

    materials = archived_materials()
    plan, script, review = map(read, ("accepted-plan.json", "draft.json", "script-review.json"))
    fact = deepcopy(review)
    for finding in fact["findings"]:
        finding["support_labels"] = [materials.material_catalog[0]]
    inputs = dict(
        draft=dict(approved_plan=plan),
        review=dict(approved_plan=plan, draft=script),
        revise=dict(approved_plan=plan, draft=script, feedback=review),
    )
    views = {
        stage: role_payload(stage, materials, projection_version=version, **args)
        for stage, args in inputs.items()
    }
    for stage in ("reader-review", "fact-review", "revise"):
        args = dict(script=script)
        if stage == "revise":
            args.update(
                readings=dict(reader=dict(review, reading_mode="script_only"), fact=fact),
                decision=dict(recommendation="revise", reason="required_changes_from_independent_readings"),
                approved_plan=plan,
            )
        views["reading:" + stage] = reading_payload(stage, materials, projection_version=version, **args)
    return views


@pytest.mark.parametrize("version", [V6, V7])
def test_pre_change_payload_and_schema_bytes_stay_reproducible(version):
    tag = version.rsplit("/", 1)[1]
    expected = json.loads((ROOT / f"backend/tests/fixtures/role-projection-v{tag}.hashes.json").read_text())
    views = historical_views(version)
    assert {stage: wire_hash(view) for stage, view in views.items()} == expected["payloads"]
    for stage, hash_value in expected["schemas"].items():
        frozen = response_schema(stage, "editorial-role-packets/1", projection_version=version)
        assert wire_hash(frozen) == hash_value
        if not stage.startswith("plan-review"):
            assert response_schema(stage, "editorial-role-packets/1", projection_version=V8) == frozen


@pytest.mark.parametrize("case", ["snow-pump", "gps-clock"])
def test_real_current_previews_keep_exact_route_and_isolate_the_cold_reader(case):
    packet = EditorialInput.model_validate_json(
        (ROOT / "evals/editorial-inputs/current" / case / "shared.json").read_bytes()
    )
    if case == "snow-pump":
        plan = json.loads(
            (ROOT / "evals/reports/2026-11-07-snow-full-episode/accepted-plan.json").read_text()
        )
    else:
        plan = json.loads((ROOT / "evals/editorial-inputs/current/gps-clock/fixed-plan.json").read_text())[
            "plan"
        ]
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    _, _, _, profile, _, reader, fact, _ = fixture()
    script = dict(
        utterances=[dict(speaker=s.label, text="Тестовая речь. " * 30) for s in packet.assignment.speakers]
    )
    for review in (reader, fact):
        for finding in review["findings"]:
            finding["anchors"] = [dict(utterance_index=0, quote="Тестовая речь.")]
    fact["findings"][0]["support_labels"] = [materials.material_catalog[0]]
    writer = payload(preview_request(uuid4(), "draft", profile, materials, approved_plan=plan))
    cold = payload(preview_request(uuid4(), "reader-review", profile, materials, draft=script))
    rewrite = payload(
        preview_request(
            uuid4(),
            "revise-1",
            profile,
            materials,
            approved_plan=plan,
            draft=script,
            reader_feedback=reader,
            fact_feedback=fact,
        )
    )
    assert writer["story"]["route_summary"] == plan["plan"]["route_summary"]
    assert writer["story"]["authority"] == "revisable_authorial_decisions"
    assert [act["suggested_priority"] for act in writer["story"]["acts"]] == [
        act["priority"] for act in plan["plan"]["acts"]
    ]
    assert all("priority" not in act for act in writer["story"]["acts"])
    assert rewrite["editorial_frame"]["route_summary"] == plan["plan"]["route_summary"]
    assert writer["duration_context"]["current_text"] is None
    assert set(cold) == {"script", "audience", "format", "genre", "output_language", "duration_context"}
    assert cold["duration_context"] == duration_context(packet.assignment, script)
    assert rewrite["duration_context"] == duration_context(packet.assignment, script, include_requested=False)
    assert cold["duration_context"]["requested"] == packet.assignment.duration.model_dump(mode="json")
    assert ("estimate_basis" in cold["duration_context"]) == (case == "snow-pump")
    assert writer["assignment"]["question"] == packet.assignment.question
    assert "assignment" not in cold and "editorial_frame" not in cold
    refs = ModelReferences.create(packet)
    fact_wire = payload(preview_request(uuid4(), "fact-review", profile, materials, draft=script))
    assert fact_wire["support"][0]["card_id"] == refs.aliases[0]
    assert "duration_context" not in fact_wire and "story" not in fact_wire


@pytest.mark.parametrize("planning", [False, True])
def test_positive_observations_keep_reasons_and_required_findings_stay_whole(planning):
    _, _, _, _, _, reader, _, review = fixture()
    feedback = review if planning else reader
    locator = (
        dict(targets=["/plan/opening/approach"])
        if planning
        else dict(anchors=[dict(utterance_index=0, quote="Старый абзац.")])
    )
    feedback["strengths"] = [
        dict(**locator, reason="Полезен контраст; порядок и формулировки можно изменить.\n" * 50)
    ]
    feedback["findings"][0]["requested_change"] = "Сжать весь обмен, сохранив причинную связь.\n" * 100
    before = deepcopy(feedback)
    projected = actionable_feedback(feedback, planning=planning)
    assert projected["positive_observations"] == feedback["strengths"]
    assert projected["findings"] == feedback["findings"]
    assert "retain_passages" not in projected and "retain_sections" not in projected
    assert feedback == before


def test_duration_reference_is_text_only_and_does_not_extend_to_other_languages():
    materials, *_ = fixture()
    assignment = materials.packet.assignment
    script = dict(utterances=[dict(speaker="NOT COUNTED", text="слово " * 2466)])
    view = duration_context(assignment, script)
    assert view["current_text"]["word_count"] == 2466
    assert view["estimated_word_range"] == dict(min=1430, max=1980)
    assert view["estimated_word_target"] == 1650
    assert view["estimated_current_seconds"] == 1346
    assert view["audio_measured"] is False
    assert "estimate_basis" not in duration_context(
        assignment.model_copy(update=dict(output_language="en")), script
    )
    assert "estimate_basis" not in duration_context(
        assignment.model_copy(update=dict(format="dialogue")), script
    )


def test_all_five_archived_essential_acts_are_suggestions_without_new_client_requirements():
    archive = json.loads((ROOT / "evals/reports/2026-11-07-snow-short-editorial/planning.json").read_text())
    plan = archive["plan-revise"].get("output", archive["plan-revise"])
    packet = EditorialInput.model_validate_json(
        (ROOT / "evals/editorial-inputs/current/snow-pump/shared.json").read_bytes()
    )
    assert packet.assignment.required_topics == []
    assert len(plan["plan"]["acts"]) == 5
    assert all(act["priority"] == "essential" for act in plan["plan"]["acts"])
    plan = ModelReferences.create(packet).translate(plan, inbound=True)
    original = deepcopy(plan)
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    visible = role_payload("draft", materials, approved_plan=plan)
    assert visible["assignment"]["required_topics"] == []
    assert visible["story"]["authority"] == "revisable_authorial_decisions"
    assert all(
        act["suggested_priority"] == "essential" and "priority" not in act for act in visible["story"]["acts"]
    )
    assert plan == original
