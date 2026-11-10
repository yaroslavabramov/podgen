"""Ending tasks change only descriptions; saved runs and all public shapes survive."""

from copy import deepcopy

import pytest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import stage
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing import ending_guidance
from podgen.writing.behavior import resolve_behavior
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.projection_versions import V8, V9
from podgen.writing.revision.scopes import FactAssessmentV2
from pydantic import ValidationError

from tests.revision_cases import scope
from tests.source_excerpt_cases import Captured, CaptureGateway, data_and_profile, release


def without_descriptions(value):
    if isinstance(value, dict):
        return {k: without_descriptions(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [without_descriptions(v) for v in value]
    return value


def test_ending_tasks_change_only_planning_descriptions_and_leave_other_roles_exact():
    behavior = resolve_behavior("production/1")
    assert behavior.planning_ending_guidance == ending_guidance.VERSION
    for name in (
        "existing/1",
        "narrative-rebuild/1",
        "narrative-rebuild/2",
        "editorial-candidate/1",
        "editorial-candidate/2",
    ):
        assert resolve_behavior(name).planning_ending_guidance is None
    for name in (
        "plan",
        "plan-revise",
        "plan-review",
        "plan-review-revised",
        "draft",
        "revise",
        "reader-review",
        "fact-review",
    ):
        options = dict(projection_version=V9, reader_schema_version=behavior.reader_schema)
        old = response_schema(name, "editorial-role-packets/1", **options)
        current = response_schema(
            name, "editorial-role-packets/1", planning_ending_guidance=ending_guidance.VERSION, **options
        )
        assert without_descriptions(current) == without_descriptions(old)
        if name in ("plan", "plan-revise"):
            ending = current["$defs"]["PlanningFinalMoment"]["properties"]
            assert (
                "composition and spoken wording remain the writer's task"
                in ending["last_moment"]["description"]
            )
            from podgen.writing.compact_editorial_descriptions import PLANNER_ENDING

            assert ending["why_end_here"]["description"] == PLANNER_ENDING["why_end_here"]
            assert (
                "not a recap"
                in old["$defs"]["PlanningFinalMoment"]["properties"]["last_moment"]["description"]
            )
        elif name.startswith("plan-review"):
            assert "listening experience" in current["properties"]["listener_answer"]["description"]
            assert current != old
        else:
            assert current == old


@pytest.mark.parametrize(
    "pin",
    [
        None,
        ending_guidance.LEGACY_VERSION,
        ending_guidance.SPEECH_VERSION,
        ending_guidance.NEUTRAL_VERSION,
        ending_guidance.CONTEXT_VERSION,
        ending_guidance.VERSION,
    ],
)
def test_actual_planner_release_preview_and_live_use_the_same_pinned_descriptions(monkeypatch, pin):
    materials, _, _, _, _, _, _, _, profile = data_and_profile()
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    assert manifest[ending_guidance.KEY] == ending_guidance.VERSION
    if pin is None:
        manifest.pop(ending_guidance.KEY)
    else:
        manifest[ending_guidance.KEY] = pin
    refs = run_references(materials, manifest)
    for name in ("plan", "plan-revise", "plan-review", "plan-review-revised"):
        expected = bind_schema(
            response_schema(
                name,
                command.input_contract,
                projection_version=V9,
                plan_review_schema_version=manifest.get("plan_review_schema_version"),
                planning_ending_guidance=ending_guidance.from_manifest(manifest),
            ),
            refs,
        )
        if pin != ending_guidance.VERSION:
            manifest["response_schemas"][name] = expected
        assert manifest["response_schemas"][name] == expected
    frozen = deepcopy(manifest)
    with pytest.raises(Captured):
        stage.execute_stage(
            "plan",
            journal=journal,
            gateway=CaptureGateway(),
            generation=store,
            command=command,
            manifest=manifest,
            materials=materials,
            outputs={},
            records={},
            totals=dict(input=0, output=0),
            guard=lambda: None,
        )
    live = journal.request(journal.invocation(command.run_id, "plan"))
    snapshot = PreviewSnapshot(manifest, live["instructions"])
    assert preview_request(command.run_id, "plan", profile("plan"), materials, snapshot=snapshot) == live
    assert live["text"]["format"]["schema"] == manifest["response_schemas"]["plan"]
    if pin == ending_guidance.VERSION:
        assert (
            preview_request(
                command.run_id, "plan", profile("plan"), materials, behavior_profile="production/1"
            )
            == live
        )
    assert manifest == frozen


def test_missing_pin_replays_legacy_descriptions_and_unknown_pin_fails_explicitly():
    assert (
        ending_guidance.from_manifest(dict(behavior_profile="production/1", role_projection_version=V9))
        is None
    )
    for value, projection, behavior in (
        ("unknown", V9, "production/1"),
        (ending_guidance.VERSION, V8, "production/1"),
        (ending_guidance.VERSION, V9, "narrative-rebuild/1"),
    ):
        with pytest.raises(ContractError, match="unsupported_planning_ending_guidance"):
            ending_guidance.from_manifest(
                dict(
                    behavior_profile=behavior,
                    role_projection_version=projection,
                    **{ending_guidance.KEY: value},
                )
            )


def test_fact_keep_allows_optional_findings_with_no_required_repair():
    *_, fact, _, _ = data_and_profile()
    fact["recommendation"] = "keep"
    fact["revision_scope"] = scope("none")
    for finding in fact["findings"]:
        finding["priority"] = "optional"
    assert FactAssessmentV2.model_validate(fact).recommendation == "keep"
    fact["findings"][0]["priority"] = "important"
    with pytest.raises(ValidationError, match="keep_cannot_require_revision"):
        FactAssessmentV2.model_validate(fact)
