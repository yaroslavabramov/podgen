"""Saved metadata calibration, compact real requests and historical serialization."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from podgen.audio.synthesis.contracts import SparseDirection, SynthesisProfile
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.episodes.production.duration_reference import resolve, validate
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.role_request import compile_reading_request, compile_role_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.behavior import resolve_behavior
from podgen.writing.duration_reference import VoiceDurationEstimate
from podgen.writing.editorial_contract import Assignment
from podgen.writing.projection_versions import V9
from tests.test_v5_role_wire import fixture


def synthesis():
    return SynthesisProfile(provider="gemini", model_id="gemini-3.8-flash-tts", voice_id="ru-ru-concierge-1")


def estimate():
    return VoiceDurationEstimate(
        synthesis=synthesis(),
        direction=SparseDirection(),
        script_word_count=1653,
        sample_frames=35122560,
        sample_rate_hz=48000,
    )


class MetadataRepo:
    """Only JSON methods exist: no object store, audio API or history traversal."""

    def __init__(self):
        self.refs = [RuntimeRef(record_kind="tool_execution", record_id=uuid4()) for _ in range(3)]
        script = dict(utterances=[dict(speaker="Host", text="One two three four")])
        common = dict(
            configuration_hash="a" * 64,
            source_ref=self.refs[2].model_dump(mode="json"),
            source_script_ref=self.refs[2].model_dump(mode="json"),
            source_script_hash=digest(script),
            coverage=dict(full_script=True, duplicates=[], gaps=[], utterances=1),
        )
        self.result = dict(
            common,
            contract="audio-evaluation-assembly-result/1",
            manifest_ref=self.refs[1].model_dump(mode="json"),
            metrics=dict(full_decode=True, sample_frames=4000, sample_rate_hz=1000),
        )
        self.manifest = dict(
            common,
            contract="audio-evaluation-assembly-manifest/1",
            profile=dict(synthesis=synthesis().model_dump(mode="json")),
            direction=dict(style=""),
        )
        self.script = dict(output=script)
        self.calls = []

    def get_runtime(self, project, ref):
        assert ref == self.refs[0]
        self.calls.append((ref,))
        return deepcopy(self.result)

    def get_runtime_many(self, project, refs):
        assert refs == self.refs[1:]
        self.calls.append(tuple(refs))
        return deepcopy([self.manifest, self.script])


def test_exact_saved_measurement_uses_two_bounded_authorized_reads():
    repo = MetadataRepo()
    ref = resolve(repo, uuid4(), repo.refs[0], synthesis())
    assert ref.estimate.words_for(1800) == 1800
    assert ref.estimate.seconds_for(4) == 4
    assert list(map(len, repo.calls)) == [1, 2]
    validate(repo, uuid4(), ref, synthesis())
    with pytest.raises(ContractError, match="measurement_changed"):
        validate(repo, uuid4(), ref.model_copy(update=dict(estimate=estimate())), synthesis())


@pytest.mark.parametrize(
    "change", ["voice", "options", "direction", "coverage", "digest", "decode", "frames"]
)
def test_measurement_rejects_mismatched_or_incomplete_metadata(change):
    repo = MetadataRepo()
    if change in ("voice", "options"):
        repo.manifest["profile"]["synthesis"]["voice_id" if change == "voice" else "options"] = (
            "another-voice" if change == "voice" else dict(speed=1.2)
        )
    elif change == "direction":
        repo.manifest["direction"]["style"] = "whisper"
    elif change == "coverage":
        for row in (repo.result, repo.manifest):
            row["coverage"]["full_script"] = False
    elif change == "digest":
        repo.script["output"]["utterances"][0]["text"] = "Changed"
    elif change == "decode":
        repo.result["metrics"]["full_decode"] = False
    else:
        repo.result["metrics"]["sample_frames"] = 0
    with pytest.raises((ContractError, ValidationError)):
        resolve(repo, uuid4(), repo.refs[0], synthesis())


def materials_with_estimate():
    materials, references, plan, profile, script, reader, fact, review = fixture()
    data = materials.packet.assignment.model_dump(mode="json")
    data.update(
        format="solo",
        output_language="ru",
        duration=dict(min_seconds=1800, target_seconds=1800, max_seconds=1980),
    )
    data["duration_estimate"] = estimate().model_dump(mode="json")
    assignment = Assignment.model_validate(data)
    packet = materials.packet.model_copy(update=dict(assignment=assignment))
    return EditorialMaterials(packet, materials.shared_ref, ()), plan, profile, script, reader, fact, review


@pytest.mark.parametrize("stage", ["plan", "plan-review", "plan-revise", "draft", "reader-review", "revise"])
def test_real_production_compilers_expose_only_the_derived_duration_context(stage):
    materials, plan, profile, script, reader, fact, review = materials_with_estimate()
    behavior = resolve_behavior("production/1")
    assert behavior.planning_duration is None
    schema = response_schema("draft", "editorial-role-packets/1", projection_version=V9)
    common = dict(projection_version=V9, author_plan_handoff=behavior.author_plan_handoff)
    if stage in ("reader-review", "revise"):
        body = compile_reading_request(
            uuid4(),
            stage,
            profile,
            materials,
            "English instructions",
            schema,
            script=script,
            approved_plan=plan,
            readings=dict(reader=reader, fact=fact),
            decision=dict(recommendation="revise"),
            **common,
        )
    else:
        inputs = (
            dict(approved_plan=plan)
            if stage == "draft"
            else ({} if stage == "plan" else dict(candidate=plan))
        )
        if stage == "plan-revise":
            inputs["plan_review"] = review
        body = compile_role_request(
            uuid4(),
            stage,
            profile,
            materials,
            "English instructions",
            schema,
            planning_duration_contract=behavior.planning_duration,
            **common,
            **inputs,
        )
    payload = request_payload(body)
    assert payload["duration_context"]["estimated_word_target"] == 4066
    assert payload["duration_context"]["audio_measured"] is False
    import json

    encoded = json.dumps(payload)
    for private in (
        "duration_estimate",
        "sample_frames",
        "voice_id",
        "script_word_count",
        "reference_recording",
    ):
        assert private not in encoded


def test_old_assignment_shape_and_language_scope():
    materials, *_ = fixture()
    assignment = materials.packet.assignment
    raw = assignment.model_dump(mode="json")
    assert "duration_estimate" not in raw
    assert Assignment.model_validate(dict(raw, duration_estimate=None)).model_dump(mode="json") == raw
    for language in ("zh", "ja"):
        with pytest.raises(ValidationError, match="duration_estimate_assignment_mismatch"):
            Assignment.model_validate(
                dict(
                    raw,
                    format="solo",
                    output_language=language,
                    duration_estimate=estimate().model_dump(mode="json"),
                )
            )


def test_actual_targeted_research_request_omits_internal_estimate(monkeypatch):
    from podgen.research.materials import research_requests as module

    materials, *_ = materials_with_estimate()
    captured = []

    class Journal:
        def __init__(self, repo, project, run_id):
            self.run_id = run_id

        def get(self, key):
            return dict(config=dict(instructions="Research", profile={})) if key == "manifest" else None

        def ref(self, key):
            return RuntimeRef(record_kind="tool_execution", record_id=self.run_id)

    class Config:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(module, "ResearchJournal", Journal)
    monkeypatch.setattr(module, "ResearchConfig", Config)
    monkeypatch.setattr(module, "prepare", lambda *args: dict(request_hash="hash"))
    store = SimpleNamespace(
        repo=None,
        project=uuid4(),
        put=lambda *args: None,
        ref=lambda *args: RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
    )
    resolver = SimpleNamespace(preparation=dict(research_run_id=str(uuid4())), materials=materials)
    module.resolve_research(
        resolver, store, "key", dict(question="Question", reason="Reason"), uuid4(), lambda: None
    )
    expected = materials.packet.assignment.model_dump(mode="json")
    expected.pop("duration_estimate")
    assert captured[0]["assignment"]["episode_assignment"] == expected


@pytest.mark.parametrize("stage", ["plan", "plan-review", "plan-revise"])
def test_production_preview_uses_unchanged_behavior_with_new_estimate(stage):
    from podgen.runtime.evaluation.preview import preview_request

    materials, plan, profile, _, _, _, review = materials_with_estimate()
    profile.model_revision_or_alias = "gpt-6.1-terra" if stage == "plan-review" else "gpt-6.1-sol"
    profile.context_limit = 200000
    profile.output_limit = 24576
    profile.generation_parameters["reasoning"]["effort"] = "medium"
    inputs = {} if stage == "plan" else dict(candidate=plan)
    if stage == "plan-revise":
        inputs["plan_review"] = review
    body = preview_request(uuid4(), stage, profile, materials, behavior_profile="production/1", **inputs)
    assert request_payload(body)["duration_context"]["estimated_word_target"] == 4066


@pytest.mark.parametrize("language", ["zh", "ja"])
def test_reference_contract_rejects_unsupported_unit_before_preparation(language):
    # Even a future expanded synthesis contract must not admit unsupported units.
    profile = synthesis().model_copy(update=dict(output_language=language))
    with pytest.raises(ValidationError, match="duration_estimate_requires_word_count_language"):
        VoiceDurationEstimate(
            synthesis=profile,
            direction=SparseDirection(),
            script_word_count=4,
            sample_frames=4000,
            sample_rate_hz=1000,
        )
