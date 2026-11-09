"""Bounded edits need current intent; archived plans and journals remain lossless."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation import reading_stage
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing import author_plan
from podgen.writing.revision.context import COMPACT_VERSION, KEY, REQUIRED_VERSION
from podgen.writing.revision.frame import FIELDS
from podgen.writing.story_views import selected_labels

from tests.revision_cases import composition
from tests.source_excerpt_cases import Captured, CaptureGateway, release
from tests.test_required_revision_assignment import case


@pytest.mark.parametrize(
    "scale,mode", [("local", "targeted"), ("sequence", "sequence"), ("whole_episode", "structural")]
)
def test_live_preview_current_frame_sources_and_cache_survive_both_rewrites(monkeypatch, scale, mode):
    materials, plan, script, original_readings, profile = case(scale)
    plan["plan"]["duration_strategy"]["assumptions"] = ["DETAILED_PLAN_ONLY"]
    frozen_plan = deepcopy(plan)
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    assert manifest[KEY] == COMPACT_VERSION
    assert manifest["prompt_cache_layout"] == "stable-role-prefix/1"
    old = deepcopy(manifest)
    old[KEY] = REQUIRED_VERSION
    cached = None
    starting = author_plan.story(plan)
    for number in (1, 2):
        name = f"revise-{number}"
        state = composition("Latest") if number == 2 else None
        if state is not None:
            state["decisions"] = ["ARCHIVED_DECISION_JOURNAL"]
            state["unresolved_questions"] = [dict(question="Still open?", why_needed="Current question.")]
        readings = deepcopy(original_readings)
        if number == 1:
            for finding in readings["fact"]["findings"]:
                finding["support_labels"] = [sorted(selected_labels(plan, materials.packet))[0]]
        before = deepcopy((readings, state))
        source = journal.save(dict(output=script, composition=state))
        refs = {
            origin: journal.save(dict(output=review, script_ref=source, script_hash=digest(script)))
            for origin, review in readings.items()
        }
        with pytest.raises(Captured):
            reading_stage.execute_reading_stage(
                name,
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
        body = journal.request(journal.invocation(command.run_id, name))
        inputs = dict(
            draft=script,
            approved_plan=plan,
            reader_feedback=readings["reader"],
            fact_feedback=readings["fact"],
        )
        assert (
            preview_request(
                command.run_id,
                name,
                profile(name),
                materials,
                snapshot=PreviewSnapshot(manifest, body["instructions"], script=script, composition=state),
                **inputs,
            )
            == body
        )
        previous = preview_request(
            command.run_id,
            name,
            profile(name),
            materials,
            snapshot=PreviewSnapshot(old, body["instructions"], script=script, composition=state),
            **inputs,
        )
        value, prior = request_payload(body), request_payload(previous)
        frame = value["editorial_frame"]
        assert set(frame) == set(FIELDS)
        expected = starting if state is None else dict(starting, **state)
        assert frame == {key: expected[key] for key in FIELDS}
        assert "decisions" not in frame
        assert "ARCHIVED_DECISION_JOURNAL" not in json.dumps(body)
        if mode == "structural":
            assert value["accepted_plan"] == prior["accepted_plan"]
            assert "DETAILED_PLAN_ONLY" in json.dumps(body)
        else:
            assert "accepted_plan" not in value
            assert "DETAILED_PLAN_ONLY" not in json.dumps(body)
        for key in ("revision_tasks", "revision_operation", "support"):
            assert value[key] == prior[key]
        assert body["instructions"] == previous["instructions"]
        assert body["model"] == previous["model"] and body["text"] == previous["text"]
        parts = body["input"][0]["content"]
        prefix = parts[: 1 + max(i for i, part in enumerate(parts) if "prompt_cache_breakpoint" in part)]
        assert any(json.loads(part["text"]).get("support") for part in prefix)
        assert all("editorial_frame" not in json.loads(part["text"]) for part in prefix)
        if cached is not None:
            assert prefix == cached
        cached = prefix
        assert journal.read(RuntimeRef.model_validate(source))["composition"] == state
        assert (readings, state) == before
    assert plan == frozen_plan


def test_every_v2_rewrite_reproduces_the_complete_pre_change_request_bytes(monkeypatch):
    expected = json.loads((Path(__file__).parent / "fixtures/revision-context-v2.hashes.json").read_text())
    for scale in ("local", "sequence", "whole_episode"):
        materials, plan, script, readings, profile = case(scale)
        _, _, _, manifest = release(monkeypatch, materials, profile)
        manifest[KEY] = REQUIRED_VERSION
        for number in (1, 2):
            stage = f"revise-{number}"
            state = composition("Latest") if number == 2 else None
            body = preview_request(
                UUID("00000000-0000-4000-8000-000000000001"),
                stage,
                profile(stage),
                materials,
                snapshot=PreviewSnapshot(
                    manifest, "Frozen author instructions.", script=script, composition=state
                ),
                draft=script,
                approved_plan=plan,
                reader_feedback=readings["reader"],
                fact_feedback=readings["fact"],
            )
            assert (
                hashlib.sha256(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest()
                == expected[f"{scale}/{stage}"]
            )
            value = request_payload(body)
            assert value["accepted_plan"]
            if number == 2:
                assert value["editorial_frame"]["decisions"] == state["decisions"]


def test_standalone_rewrite_without_plan_keeps_fact_cards_in_the_changing_part():
    materials, _, script, readings, profile = case("local")
    body = preview_request(
        UUID("00000000-0000-4000-8000-000000000001"),
        "revise-1",
        profile("revise-1"),
        materials,
        draft=script,
        reader_feedback=readings["reader"],
        fact_feedback=readings["fact"],
    )
    value = request_payload(body)
    assert value["support"] and value["revision_tasks"]
    assert "accepted_plan" not in value and "editorial_frame" not in value
    parts = body["input"][0]["content"]
    stop = max(i for i, part in enumerate(parts) if "prompt_cache_breakpoint" in part)
    assert all(not json.loads(part["text"]).get("support") for part in parts[: stop + 1])
    assert json.loads(parts[-1]["text"])["additional_support"] == value["support"]
