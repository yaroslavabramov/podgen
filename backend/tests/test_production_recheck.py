"""Exact transmitted work, old-version reconstruction and production replay without inference or a DB."""

import json
from copy import deepcopy

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import reading_stage, recheck_source
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.runtime.evaluation.wire_budget import PRODUCTION_INPUT_BUDGET, check_visible
from podgen.writing.projections import CONTRACT
from podgen.writing.reader_schema import COMPACT_VERSION
from podgen.writing.revision import task_verification, verification

from tests.source_excerpt_cases import Captured, CaptureGateway
from tests.source_excerpt_cases import release_before_reading_audit as release
from tests.test_required_revision_assignment import case
from tests.test_revision_verification import reconstruct


def complete(journal, row, value):
    ref = journal.save(
        dict(value, request_ref=dict(record_kind="tool_execution", record_id=str(row["request_record_id"])))
    )
    row.update(status="completed", output_record_id=RuntimeRef.model_validate(ref).record_id)
    return ref


def previous_review(journal, run_id, stage, script, script_ref, review):
    row = journal.prepare(run_id, stage, {}, dict(script_ref=script_ref, script_hash=digest(script)))
    return complete(
        journal,
        row,
        dict(
            output=review, stage=stage, run_id=str(run_id), script_ref=script_ref, script_hash=digest(script)
        ),
    )


@pytest.mark.parametrize(
    "scale,mode", [("local", "targeted"), ("sequence", "sequence"), ("whole_episode", "structural")]
)
def test_actual_writer_assignment_reaches_runtime_preview_and_saved_verification(monkeypatch, scale, mode):
    materials, plan, script, readings, profile = case(scale)
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    journal.output = lambda row: journal.records[row["output_record_id"]]
    assert manifest[verification.KEY] == verification.TASK_VERSION
    for number in (1, 2):
        before = deepcopy(script)
        source = journal.save(dict(output=before))
        old_reader = "reader-review" if number == 1 else "reader-review-1"
        refs = dict(
            reader=previous_review(journal, command.run_id, old_reader, before, source, readings["reader"])
        )
        refs["fact"] = journal.save(
            dict(output=readings["fact"], script_ref=source, script_hash=digest(before))
        )
        rewrite = f"revise-{number}"
        with pytest.raises(Captured):
            reading_stage.execute_reading_stage(
                rewrite,
                journal=journal,
                gateway=CaptureGateway(),
                generation=store,
                command=command,
                manifest=manifest,
                materials=materials,
                totals=dict(input=0, output=0),
                guard=lambda: None,
                script=before,
                script_ref=source,
                readings=readings,
                reading_refs=refs,
                decision=combine(readings),
                approved_plan=plan,
            )
        writer_row = journal.invocation(command.run_id, rewrite)
        actual_writer_input = request_payload(journal.request(writer_row))
        assert "revision_guidance" not in actual_writer_input
        assert actual_writer_input["revision_operation"]["mode"] == mode
        # Simulate a materialized complete writer result; the writer input above is the real compiler.
        script = deepcopy(before)
        script["utterances"][0]["text"] += f" Changed connection {number}."
        current_ref = complete(
            journal,
            writer_row,
            dict(
                output=script,
                stage=rewrite,
                run_id=str(command.run_id),
                script_ref=source,
                script_hash=digest(before),
                reading_refs=refs,
            ),
        )
        stage = f"reader-review-{number}"
        _, comparison = recheck_source.load(
            journal,
            command.run_id,
            stage,
            version=verification.TASK_VERSION,
            script_ref=current_ref,
            script=script,
        )
        assert reconstruct(comparison, script) == before
        assert comparison["revision_operation"] == actual_writer_input["revision_operation"]
        assert [row["id"] for row in comparison["assignments"]] == ["R1", "F1"]
        assert [{k: v for k, v in task.items() if k != "id"} for task in comparison["assignments"]] == [
            {k: v for k, v in task.items() if k in (*task_verification.TASK_FIELDS, "preserve")}
            for task in actual_writer_input["revision_tasks"]
        ]
        assert comparison["previous_reader_decision"] == {
            key: readings["reader"][key] for key in task_verification.DECISION_FIELDS
        }
        assert "OPTIONAL_ACTION" not in json.dumps(comparison)
        with pytest.raises(Captured):
            reading_stage.execute_reading_stage(
                stage,
                journal=journal,
                gateway=CaptureGateway(),
                generation=store,
                command=command,
                manifest=manifest,
                materials=materials,
                totals=dict(input=0, output=0),
                guard=lambda: None,
                script=script,
                script_ref=current_ref,
            )
        row = journal.invocation(command.run_id, stage)
        body = journal.request(row)
        snapshot = PreviewSnapshot(
            manifest, body["instructions"], script=script, revision_comparison=comparison
        )
        assert (
            preview_request(command.run_id, stage, profile(stage), materials, snapshot=snapshot, draft=script)
            == body
        )
        check_visible(body, CONTRACT, max_visible_bytes=PRODUCTION_INPUT_BUDGET["visible_bytes"])
        exposed = request_payload(body)
        assert set(exposed) == {
            "script",
            "audience",
            "format",
            "genre",
            "output_language",
            "duration_context",
            "revision_check",
        }
        assert set(exposed["revision_check"]) == {
            "previous_script_in_order",
            "assignments",
            "revision_operation",
            "previous_reader_decision",
        }
        for forbidden in (
            "review_source_passages",
            "evidence_refs",
            "support_refs",
            "source_read_refs",
            "GENERAL_TIMING_ASSESSMENT",
        ):
            assert forbidden not in json.dumps(exposed)
        result = dict(
            readings["reader"],
            reading_mode="informed",
            effect_on_episode="The connection now works.",
            recommendation="keep",
            findings=[],
            revision_scope=dict(
                scale="none", reason="No required repair.", anchors=[], dependent_passages=[]
            ),
            assignment_results=[
                dict(
                    assignment_id=key,
                    outcome="achieved",
                    judgment="The words resolve the task.",
                    followup_finding=None,
                )
                for key in ("R1", "F1")
            ],
        )
        context = journal.records[row["request_record_id"]]["context"]
        recheck_source.verify_saved(
            journal,
            command.run_id,
            stage,
            current_ref,
            script,
            row,
            context,
            result,
            version=verification.TASK_VERSION,
        )
        corrupt = deepcopy(context)
        corrupt["revision_verification"]["comparison_hash"] = "not-the-transmitted-work"
        with pytest.raises(Conflict, match="binding_mismatch"):
            recheck_source.verify_saved(
                journal,
                command.run_id,
                stage,
                current_ref,
                script,
                row,
                corrupt,
                result,
                version=verification.TASK_VERSION,
            )
        previous_record = journal.read(RuntimeRef.model_validate(refs["reader"]))
        previous_record["script_hash"] = "wrong-script"
        with pytest.raises(Conflict, match="previous_reader_mismatch"):
            recheck_source.load(journal, command.run_id, stage, version=verification.TASK_VERSION)
        previous_record["script_hash"] = digest(before)


def test_recheck_contract_isolated_sources_and_independent_copies():
    script = dict(utterances=[dict(speaker="A", text="Full speech.")])
    task = dict(
        origin="fact",
        anchors=[dict(utterance_index=0, quote="Full speech.")],
        problem="Exact diagnosis.",
        action="Exact assignment.",
        preserve=["Complete condition."],
        evidence_refs=[0],
        source_read_refs=["P1"],
    )
    source = dict(
        revision_tasks=[task],
        revision_operation=dict(mode="structural", script_version=1),
        review_source_passages=[dict(text="SECRET PRIMARY SOURCE")],
    )
    decision = dict(
        recommendation="revise",
        listener_answer="Complete assessment.",
        strengths=[],
        important_limits=["Complete limit."],
        revision_scope={"scale": "whole_episode"},
        findings=[dict(observation="DUPLICATE")],
    )
    comparison = task_verification.comparison(script, script, source, decision)
    assert comparison["assignments"] == [
        dict(id="F1", **{key: task[key] for key in (*task_verification.TASK_FIELDS, "preserve")})
    ]
    assert "SECRET" not in json.dumps(comparison) and "DUPLICATE" not in json.dumps(comparison)
    source["revision_tasks"][0]["preserve"].clear()
    decision["important_limits"].clear()
    assert comparison["assignments"][0]["preserve"] == ["Complete condition."]
    assert comparison["previous_reader_decision"]["important_limits"] == ["Complete limit."]


def test_v1_schema_frozen_while_v2_uses_pinned_production_descriptions(monkeypatch):
    from podgen.writing import compact_editorial_descriptions, reader_schema

    legacy_hash = "780ca0614d53f4df896dca830c1ae3f866261889e7abe962615048e2b0678983"
    assert digest(verification.schema()) == legacy_hash
    old = response_schema("reader-review-1", CONTRACT, revision_verification_contract=verification.VERSION)
    new = response_schema(
        "reader-review-1",
        CONTRACT,
        revision_verification_contract=verification.TASK_VERSION,
        reader_schema_version=COMPACT_VERSION,
    )
    assert set(new["properties"]) == set(old["properties"])
    assert (
        new["properties"]["findings"]["description"]
        == compact_editorial_descriptions.READER_FIELDS["findings"]
    )
    monkeypatch.setitem(reader_schema.FIELDS, "listener_answer", "Future wording.")
    assert digest(verification.schema(reader_schema_version=COMPACT_VERSION)) == legacy_hash
    for stage in ("reader-review", "fact-review", "fact-review-1", "fact-review-2"):
        assert response_schema(stage, CONTRACT, reader_schema_version=COMPACT_VERSION) == response_schema(
            stage,
            CONTRACT,
            reader_schema_version=COMPACT_VERSION,
            revision_verification_contract=verification.TASK_VERSION,
        )
