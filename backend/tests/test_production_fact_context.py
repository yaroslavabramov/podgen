"""New fact views are isolated, frozen on resume and cached before the changing speech."""

from copy import deepcopy

import pytest

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import reading_stage
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing import fact_context
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.projections import evidence_view
from podgen.writing.revision.contracts import FACT_VERSION
from tests.revision_cases import scope
from tests.source_excerpt_cases import Captured, CaptureGateway, data_and_profile, release


def test_actual_fact_rounds_match_preview_and_cache_all_evidence_before_script(monkeypatch):
    materials, _, _, _, script, _, _, _, profile = data_and_profile()
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    assert manifest[fact_context.KEY] == fact_context.SIGNATURE_VERSION
    assert manifest["fact_review_schema_version"] == fact_context.READ_SCHEMA_VERSION
    refs = run_references(materials, manifest)
    before = deepcopy(materials.packet.model_dump(mode="json"))
    prefix = None
    for stage in ("fact-review", "fact-review-1", "fact-review-2"):
        source = journal.save(dict(output=script))
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
                script_ref=source,
            )
        body = journal.request(journal.invocation(command.run_id, stage))
        assert (
            preview_request(
                command.run_id,
                stage,
                profile(stage),
                materials,
                snapshot=PreviewSnapshot(manifest, body["instructions"]),
                draft=script,
            )
            == body
        )
        value = request_payload(body)
        assert not {"support", "necessary_limits"}.intersection(value)
        assert all("limitations" not in card for card in value["context"])
        assert value["evidence"] == refs.translate(evidence_view(materials.packet))
        assert all(label.startswith("M") for note in value["research_cautions"] for label in note["supports"])
        assert all(label.startswith("M") for row in value["source_bibliography"] for label in row["supports"])
        parts = body["input"][0]["content"]
        static = parts[: 1 + max(i for i, part in enumerate(parts) if "prompt_cache_breakpoint" in part)]
        import json

        assert all("script" not in json.loads(part["text"]) for part in static)
        assert "research_cautions" in json.loads(static[-1]["text"])
        if prefix is not None:
            assert prefix == static
        prefix = static
    assert materials.packet.model_dump(mode="json") == before


def test_old_fact_pin_replays_previous_context_schema_and_other_stages_are_identical(monkeypatch):
    materials, plan, _, _, script, reader, fact, review, profile = data_and_profile()
    for assessment in (reader, fact):
        assessment["revision_scope"] = scope(
            "local", anchors=[dict(utterance_index=0, quote="First observation.")]
        )
    store, _, command, current = release(monkeypatch, materials, profile)
    old = deepcopy(current)
    old.pop(fact_context.KEY)
    old["fact_review_schema_version"] = FACT_VERSION
    refs = run_references(materials, old)
    for stage in ("fact-review", "fact-review-1", "fact-review-2"):
        old["response_schemas"][stage] = bind_schema(
            response_schema(stage, command.input_contract, fact_schema_version=FACT_VERSION), refs
        )
    for stage in ("plan", "plan-review", "plan-revise", "draft", "reader-review", "revise-1", "fact-review"):
        instructions = store.get(
            command.project_id, ArtifactRef.model_validate(current["prompt_refs"][stage])
        ).payload["template"]
        inputs = (
            dict(candidate=plan, plan_review=review)
            if stage == "plan-revise"
            else dict(candidate=plan)
            if stage == "plan-review"
            else dict(approved_plan=plan)
            if stage == "draft"
            else dict(draft=script, reader_feedback=reader, fact_feedback=fact, approved_plan=plan)
            if stage == "revise-1"
            else dict(draft=script)
            if stage in ("reader-review", "fact-review")
            else {}
        )
        a = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(current, instructions),
            **inputs,
        )
        b = preview_request(
            command.run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(old, instructions),
            **inputs,
        )
        if stage == "fact-review":
            legacy = request_payload(b)
            assert "support" in legacy and "necessary_limits" in legacy
            assert b["text"]["format"]["schema"] == old["response_schemas"][stage]
            assert "important_limits" == list(b["text"]["format"]["schema"]["properties"])[2]
        else:
            assert a == b
        assert (
            preview_request(
                command.run_id,
                stage,
                profile(stage),
                materials,
                snapshot=PreviewSnapshot(old, instructions),
                **inputs,
            )
            == b
        )


def test_fact_cautions_are_complete_and_each_identical_note_occurs_once():
    materials, *_ = data_and_profile()
    packet = deepcopy(materials.packet)
    note = "Research caution is not a required spoken qualification."
    packet.dossier.knowledge_cards[0].limitations.append(note)
    packet.dossier.throughline_constraints.append(note)
    values = fact_context.research_cautions(packet)
    assert len([row for row in values if row["note"] == note]) == 1
    assert next(row for row in values if row["note"] == note)["dossier_wide"] is True
    assert {row["note"] for row in values} == set(packet.dossier.throughline_constraints) | {
        text for card in packet.dossier.knowledge_cards for text in card.limitations
    }


def test_nonprimary_quotes_are_context_without_loss_and_only_production_can_enable_the_new_pin():
    materials, *_ = data_and_profile()
    packet = materials.packet.model_copy(
        update={
            "documents": [
                source.model_copy(update={"purpose": "dossier"}) for source in materials.packet.documents
            ]
        }
    )
    primary, context = fact_context.evidence_views(packet)
    assert primary == [] and context == evidence_view(packet)
    old = dict(
        behavior_profile="production/1",
        role_projection_version="editorial-role-projections/9",
        fact_review_schema_version=FACT_VERSION,
    )
    assert fact_context.from_manifest(old) is None
    for extra in (
        {fact_context.KEY: "unknown"},
        {fact_context.KEY: fact_context.VERSION},
        {
            fact_context.KEY: fact_context.VERSION,
            "fact_review_schema_version": fact_context.SCHEMA_VERSION,
            "behavior_profile": "existing/1",
        },
    ):
        with pytest.raises(ContractError, match="unsupported_fact_review_context"):
            fact_context.from_manifest(dict(old, **extra))
