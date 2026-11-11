"""Required /2 assignments cannot disappear into optional findings or a cached keep."""

import json
from copy import deepcopy
from hashlib import sha256

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation import reading_stage, recheck_source
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.runtime.evaluation.workflow import output_model
from podgen.writing.reader_schema import COMPACT_VERSION, feedback_model, snapshot_model
from podgen.writing.revision import verification as checks
from podgen.writing.roles import WriteOutput
from pydantic import ValidationError

from tests.revision_cases import scope
from tests.source_excerpt_cases import Captured, CaptureGateway
from tests.source_excerpt_cases import release_before_reading_audit as release
from tests.test_production_recheck import complete, previous_review
from tests.test_required_revision_assignment import case


def review(outcome="still_open", priority="optional", recommendation="keep", scale="none"):
    _, _, script, readings, _ = case("local")
    result = deepcopy(readings["reader"])
    result["findings"] = result["findings"][:1]
    result["findings"][0]["priority"] = priority
    result.update(
        reading_mode="informed",
        recommendation=recommendation,
        revision_scope=scope(scale, anchors=result["findings"][0]["anchors"] if scale != "none" else []),
        effect_on_episode="The assignment has been compared with the current speech.",
        assignment_results=[
            dict(
                assignment_id="R1",
                outcome=outcome,
                judgment="Specific outcome.",
                followup_finding=0 if outcome == "still_open" else None,
            )
        ],
    )
    if recommendation == "needs_material":
        result["research_requests"] = [
            dict(
                anchors=result["findings"][0]["anchors"],
                question="What is the basis?",
                why_needed="This necessary connection remains unsupported.",
            )
        ]
    return script, result


@pytest.mark.parametrize(
    "outcome,priority,recommendation,scale,valid",
    [
        ("still_open", "optional", "keep", "none", False),
        ("still_open", "optional", "revise", "local", False),
        ("still_open", "important", "keep", "none", False),
        ("still_open", "important", "revise", "none", False),
        ("still_open", "important", "revise", "local", True),
        ("still_open", "blocking", "revise", "whole_episode", True),
        ("still_open", "important", "needs_material", "none", True),
        ("solved_differently", "optional", "keep", "none", True),
        ("no_longer_needed", "optional", "keep", "none", True),
        ("achieved", "optional", "keep", "none", True),
    ],
)
def test_required_assignment_decision_matrix(outcome, priority, recommendation, scale, valid):
    script, result = review(outcome, priority, recommendation, scale)
    model = output_model("reader-review-1", revision_verification_contract=checks.TASK_VERSION)
    if not valid:
        with pytest.raises(ValidationError):
            model.model_validate(result)
        return
    parsed = model.model_validate(result)
    parsed.verify_assignments(dict(assignments=[dict(id="R1")]))
    parsed.validate_against(WriteOutput.model_validate(script))


def test_legacy_optional_assignment_and_ordinary_snapshot_models_keep_their_contracts():
    _, result = review()
    legacy = output_model("reader-review-1", revision_verification_contract=checks.VERSION)
    legacy.model_validate(result).verify_assignments(dict(assignments=[dict(id="R1")]))
    assert snapshot_model(result) is legacy
    assert snapshot_model(result, revision_verification_contract=checks.VERSION) is legacy
    assert (
        snapshot_model(result, revision_verification_contract=checks.TASK_VERSION)
        is checks.RequiredRevisionReading
    )
    _, _, _, readings, _ = case("local")
    plain = readings["reader"]
    assert snapshot_model(plain, revision_verification_contract=checks.TASK_VERSION) is feedback_model(
        COMPACT_VERSION
    )
    ancient = {key: value for key, value in plain.items() if key != "revision_scope"}
    assert snapshot_model(ancient, revision_verification_contract=checks.TASK_VERSION) is feedback_model(None)


def test_both_published_schemas_remain_exactly_the_same_bytes():
    # Captured immediately before introducing the versioned runtime validator.
    expected = {
        checks.VERSION: "cbb51b94c53c6562178e7b1ad928c381ab9d60619231c65dabfa5ac171c02283",
        checks.TASK_VERSION: "fadc9030606395acf72e79f24db89fb7477ece7b8e09aa4330b96ee384ed5dad",
    }
    for version, expected_hash in expected.items():
        schema = checks.schema(version, reader_schema_version=COMPACT_VERSION)
        assert (
            sha256(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            == expected_hash
        )
        assert schema["title"] == "RevisionReading"
        assert "RequiredRevisionReading" not in json.dumps(schema)


@pytest.mark.parametrize("pin,valid", [(None, True), (checks.VERSION, True), (checks.TASK_VERSION, False)])
def test_preview_uses_the_pinned_version_for_incoming_reader_feedback(monkeypatch, pin, valid):
    materials, plan, script, readings, profile = case("local")
    _, _, command, manifest = release(monkeypatch, materials, profile)
    _, bad = review()
    if pin is None:
        manifest.pop(checks.KEY)
    else:
        manifest[checks.KEY] = pin
    kwargs = dict(
        snapshot=PreviewSnapshot(manifest, "Frozen writer instruction.", script=script),
        draft=script,
        approved_plan=plan,
        reader_feedback=bad,
        fact_feedback=readings["fact"],
    )
    if valid:
        assert preview_request(command.run_id, "revise-2", profile("revise-2"), materials, **kwargs)
    else:
        with pytest.raises(ValidationError, match="keep_has_open_task"):
            preview_request(command.run_id, "revise-2", profile("revise-2"), materials, **kwargs)
        with pytest.raises(ValidationError, match="audit"):
            preview_request(
                command.run_id,
                "revise-2",
                profile("revise-2"),
                materials,
                **{k: v for k, v in kwargs.items() if k != "snapshot"},
            )


def test_saved_completed_recheck_is_revalidated_before_resume_can_combine_it(monkeypatch):
    materials, plan, script, readings, profile = case("local")
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    journal.output = lambda row: journal.records[row["output_record_id"]]
    source = journal.save(dict(output=script))
    refs = dict(
        reader=previous_review(journal, command.run_id, "reader-review", script, source, readings["reader"])
    )
    refs["fact"] = journal.save(dict(output=readings["fact"], script_ref=source, script_hash=digest(script)))
    common = dict(
        journal=journal,
        gateway=CaptureGateway(),
        generation=store,
        command=command,
        manifest=manifest,
        materials=materials,
        totals=dict(input=0, output=0),
        guard=lambda: None,
    )
    with pytest.raises(Captured):
        reading_stage.execute_reading_stage(
            "revise-1",
            **common,
            script=script,
            script_ref=source,
            readings=readings,
            reading_refs=refs,
            decision=combine(readings),
            approved_plan=plan,
        )
    writer = journal.invocation(command.run_id, "revise-1")
    current = complete(
        journal,
        writer,
        dict(
            output=script,
            stage="revise-1",
            run_id=str(command.run_id),
            script_ref=source,
            script_hash=digest(script),
            reading_refs=refs,
        ),
    )
    with pytest.raises(Captured):
        reading_stage.execute_reading_stage("reader-review-1", **common, script=script, script_ref=current)
    row = journal.invocation(command.run_id, "reader-review-1")
    _, bad = review()
    bad["assignment_results"].append(
        dict(assignment_id="F1", outcome="achieved", judgment="Completed.", followup_finding=None)
    )
    complete(
        journal,
        row,
        dict(
            output=bad,
            stage="reader-review-1",
            run_id=str(command.run_id),
            script_ref=current,
            script_hash=digest(script),
        ),
    )
    context = journal.read(RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"]))[
        "context"
    ]
    with pytest.raises(ValidationError, match="keep_has_open_task"):
        recheck_source.verify_saved(
            journal,
            command.run_id,
            "reader-review-1",
            current,
            script,
            row,
            context,
            bad,
            version=checks.TASK_VERSION,
        )
    # Resume must reject the saved completed response before returning a stage result;
    # the gateway is never invoked and therefore cannot hide the rejected cache.
    with pytest.raises(RunFailure, match="independent_reading_output_invalid"):
        reading_stage.execute_reading_stage("reader-review-1", **common, script=script, script_ref=current)
