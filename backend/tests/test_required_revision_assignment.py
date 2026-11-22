"""Real rewrite compilers receive located required work, not full reviews or duplicated evidence."""

from copy import deepcopy

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import reading_stage, script_revision
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing.revision.assignment import tasks
from podgen.writing.revision.context import COMPACT_VERSION, KEY, REQUIRED_VERSION, from_manifest
from tests.revision_cases import composition, scope
from tests.source_excerpt_cases import (
    Captured,
    CaptureGateway,
    data_and_profile,
    release,
    release_before_reading_audit,
)


def case(scale):
    materials, plan, _, extra, script, reader, fact, _, profile = data_and_profile()
    script["utterances"].append(
        dict(speaker=script["utterances"][0]["speaker"], text="Optional map comparison.")
    )
    first = dict(utterance_index=0, quote="First observation.")
    last = dict(utterance_index=1, quote="The result follows.")
    optional = dict(utterance_index=2, quote="Optional map comparison.")
    reader["revision_scope"] = scope(
        scale,
        anchors=[first],
        **(
            dict(sequence_ranges=[dict(anchors=[first, last], reason="Required scene order.")])
            if scale == "sequence"
            else {}
        ),
    )
    fact["revision_scope"] = scope("local", anchors=[first])
    for review in (reader, fact):
        review["findings"] = [review["findings"][0]]
        review["findings"][0]["anchors"] = [first]
        spare = deepcopy(review["findings"][0])
        spare.update(
            priority="optional",
            anchors=[optional],
            observation="OPTIONAL_PROBLEM",
            requested_change="OPTIONAL_ACTION",
        )
        review["findings"].append(spare)
        review["strengths"] = [dict(anchors=[first], reason="GENERAL_PRAISE")]
        review["listener_answer"] = "REVIEW_SUMMARY"
        review["duration_note"] = "GENERAL_TIMING_ASSESSMENT"
    return materials, plan, script, dict(reader=reader, fact=fact), profile


@pytest.mark.parametrize(
    "scale,mode", [("local", "targeted"), ("sequence", "sequence"), ("whole_episode", "structural")]
)
def test_both_real_rewrites_share_exact_tasks_permissions_and_existing_sources(monkeypatch, scale, mode):
    materials, plan, script, readings, profile = case(scale)
    original = deepcopy((plan, script, readings))
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    assert manifest[KEY] == COMPACT_VERSION
    previous_prefix = None
    for number in (1, 2):
        stage = f"revise-{number}"
        state = composition("Latest") if number == 2 else None
        script_ref = journal.save(dict(output=script, composition=state))
        reading_refs = {
            origin: journal.save(dict(output=review, script_ref=script_ref, script_hash=digest(script)))
            for origin, review in readings.items()
        }
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
                script_ref=script_ref,
                readings=readings,
                reading_refs=reading_refs,
                decision=combine(readings),
                approved_plan=plan,
            )
        wire = journal.request(journal.invocation(command.run_id, stage))
        assert (
            preview_request(
                command.run_id,
                stage,
                profile(stage),
                materials,
                snapshot=PreviewSnapshot(manifest, wire["instructions"], script=script, composition=state),
                draft=script,
                reader_feedback=readings["reader"],
                fact_feedback=readings["fact"],
                approved_plan=plan,
            )
            == wire
        )
        value = request_payload(wire)
        assert not {"revision_guidance", "decision", "craft_reference", "repair_evidence"}.intersection(value)
        assert len(value["revision_tasks"]) == 2
        for task in value["revision_tasks"]:
            finding = readings[task["origin"]]["findings"][0]
            assert task["anchors"] == finding["anchors"]
            assert task["problem"] == finding["observation"]
            assert task["action"] == finding["requested_change"]
            assert task.get("preserve", []) == finding["preserve"]
            assert set(task) <= {
                "origin",
                "anchors",
                "problem",
                "action",
                "preserve",
                "evidence_refs",
                "support_refs",
            }
        cards = {card["card_id"]: card for card in value["support"]}
        fact_task = next(task for task in value["revision_tasks"] if task["origin"] == "fact")
        assert fact_task["evidence_refs"]
        for reference in fact_task["evidence_refs"]:
            excerpts = cards[reference["card_id"]]["source_excerpts"]
            assert reference["source_excerpt_indices"] == list(range(len(excerpts)))
        operation = value["revision_operation"]
        invocation = journal.invocation(command.run_id, stage)
        server_selection = journal.records[invocation["request_record_id"]]["context"]["revision_operation"]
        assert script_revision.model_operation(server_selection) == operation
        assert operation["mode"] == mode
        assert all(target.get("utterance_index") != 2 for target in operation["targets"])
        assert all(target.get("before_utterance_index") != 3 for target in operation["targets"])
        if mode == "structural":
            assert operation["targets"] == []
            assert all(task["anchors"] for task in value["revision_tasks"])
        if mode == "sequence":
            assert operation["sequences"] == [
                dict(range_id="R1", first_utterance_index=0, last_utterance_index=1)
            ]
        if mode == "targeted":
            target = next(
                row for row in server_selection["targets"] if row.get("quote") == "First observation."
            )
            wire_edit = dict(
                edits=[dict(operation="replace", target_id=target["target_id"], text="Updated observation.")],
                composition_update=None,
            )
            updated, current, proof = script_revision.apply(
                server_selection,
                wire_edit,
                script=script,
                composition=state,
                speakers=materials.speaker_labels,
                expected_hash=digest(script),
            )
            assert updated["utterances"][2] == script["utterances"][2]
            script_revision.verify_record(
                dict(revision=proof, output=updated, composition=current),
                script=script,
                composition=state,
                readings=readings,
                speakers=materials.speaker_labels,
                version=number - 1,
                revision_context_contract=REQUIRED_VERSION,
            )
            with pytest.raises(ValueError, match="revision_target_not_offered"):
                script_revision.apply(
                    server_selection,
                    dict(
                        edits=[
                            dict(operation="insert", target_id="B3", utterances=[script["utterances"][2]])
                        ],
                        composition_update=None,
                    ),
                    script=script,
                    composition=state,
                    speakers=materials.speaker_labels,
                    expected_hash=digest(script),
                )
        blocks = wire["input"][0]["content"]
        prefix = blocks[: 1 + max(i for i, part in enumerate(blocks) if "prompt_cache_breakpoint" in part)]
        if previous_prefix is not None:
            assert prefix == previous_prefix
        previous_prefix = prefix
        for origin, ref in reading_refs.items():
            from podgen.domain.contracts.common import RuntimeRef

            assert journal.read(RuntimeRef.model_validate(ref))["output"] == readings[origin]
    assert (plan, script, readings) == original


def test_old_snapshot_preserves_full_reviews_optional_permissions_and_craft(monkeypatch):
    materials, plan, script, readings, profile = case("local")
    _, _, command, old = release_before_reading_audit(monkeypatch, materials, profile)
    old.pop(KEY)
    frozen = deepcopy(old)
    snapshot = PreviewSnapshot(old, "Pinned historical writer instructions.")
    inputs = dict(
        draft=script, approved_plan=plan, reader_feedback=readings["reader"], fact_feedback=readings["fact"]
    )
    wire = preview_request(
        command.run_id, "revise-1", profile("revise-1"), materials, snapshot=snapshot, **inputs
    )
    value = request_payload(wire)
    assert "revision_tasks" not in value and value["craft_reference"]
    assert value["revision_guidance"][0]["positive_observations"] == readings["reader"]["strengths"]
    assert value["revision_guidance"][0]["findings"][1]["priority"] == "optional"
    assert any(target.get("utterance_index") == 2 for target in value["revision_operation"]["targets"])
    assert (
        preview_request(
            command.run_id, "revise-1", profile("revise-1"), materials, snapshot=snapshot, **inputs
        )
        == wire
    )
    assert old == frozen
    draft = preview_request(command.run_id, "draft", profile("draft"), materials, approved_plan=plan)
    assert request_payload(draft)["craft_reference"] == old["craft_reference"]["text"]


def test_fact_context_without_text_gets_no_invented_excerpt_reference_and_empty_preserve_is_omitted():
    finding = dict(
        priority="important",
        anchors=[dict(utterance_index=7, quote="Exact factual claim.")],
        observation="Exact unsupported implication.",
        requested_change="Narrow the claim.",
        preserve=[],
        support_labels=["C1", "C2"],
    )
    readings = dict(reader=dict(findings=[]), fact=dict(findings=[finding]))
    result = tasks(
        readings,
        [
            dict(card_id="C1", claim="Full card basis without a visible excerpt."),
            dict(card_id="C2", source_excerpts=[dict(source="Exact title", text=" Exact quote\n")]),
        ],
    )
    assert result[0]["support_refs"] == [dict(card_id="C1")]
    assert result[0]["evidence_refs"] == [dict(card_id="C2", source_excerpt_indices=[0])]
    assert result[0]["problem"] == finding["observation"] and "preserve" not in result[0]
    with pytest.raises(ContractError, match="revision_support_card_missing"):
        tasks(readings, [])


def test_empty_required_work_is_diagnosed_before_author_request_is_prepared(monkeypatch):
    materials, plan, script, readings, profile = case("whole_episode")
    for review in readings.values():
        for finding in review["findings"]:
            finding["priority"] = "optional"
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    source = journal.save(dict(output=script, composition=None))
    refs = {
        origin: journal.save(dict(output=review, script_ref=source, script_hash=digest(script)))
        for origin, review in readings.items()
    }
    with pytest.raises(ContractError, match="revision_required_findings_missing"):
        reading_stage.execute_reading_stage(
            "revise-1",
            journal=journal,
            gateway=CaptureGateway(),
            generation=store,
            command=command,
            manifest=manifest,
            materials=materials,
            totals=dict(input=0, output=0),
            guard=lambda: None,
            script=script,
            script_ref=source,
            readings=readings,
            reading_refs=refs,
            decision=combine(readings),
            approved_plan=plan,
        )
    assert journal.invocation(command.run_id, "revise-1") is None
    with pytest.raises(ContractError, match="unsupported_revision_context_contract"):
        from_manifest(
            dict(
                revision_context_contract=REQUIRED_VERSION,
                role_projection_version="editorial-role-projections/9",
                script_revision_contract=script_revision.VERSION,
                behavior_profile="existing/1",
            )
        )


def test_required_sequence_keeps_its_whole_range_and_optional_only_review_cannot_expand_it():
    _, _, script, readings, _ = case("sequence")
    readings["reader"]["revision_scope"]["sequence_ranges"][0]["anchors"][-1] = dict(
        utterance_index=2, quote="Optional map comparison."
    )
    frozen = deepcopy(readings)
    selection = script_revision.operation(script, readings, 0, revision_context_contract=REQUIRED_VERSION)
    assert selection["mode"] == "sequence"
    assert [(row["start"], row["end"]) for row in selection["sequences"]] == [(0, 3)]
    assert all(row.get("utterance_index") != 2 for row in selection["targets"])
    assert readings == frozen
    for finding in readings["reader"]["findings"]:
        finding["priority"] = "optional"
    readings["reader"]["revision_scope"] = scope(
        "whole_episode", anchors=[dict(utterance_index=2, quote="Optional map comparison.")]
    )
    selection = script_revision.operation(script, readings, 0, revision_context_contract=REQUIRED_VERSION)
    assert selection["mode"] == "targeted"
    assert all(row.get("utterance_index") != 2 for row in selection["targets"])
