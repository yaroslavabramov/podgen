"""Small in-memory journal adapters exercising the actual released request compilers."""

from copy import copy
from types import SimpleNamespace
from uuid import uuid4

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation import manifest as manifests
from podgen.runtime.evaluation import reading_stage, stage
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import preview_references
from podgen.writing.projection_versions import V9
from tests.test_editorial_independence import payload
from tests.test_source_passage_handoff import current_materials, selected_plan
from tests.test_v5_role_wire import fixture


class Captured(Exception):
    pass


class ReleaseStore:
    def __init__(self):
        self.prompts = {}

    def create(self, project, kind, value, **kwargs):
        ref = ArtifactRef(kind=kind, artifact_id=uuid4(), revision_id=uuid4())
        record = SimpleNamespace(ref=ref, payload=value)
        self.prompts[ref.revision_id] = record
        return record

    def get(self, project, ref):
        return self.prompts[ref.revision_id]


class Journal:
    def __init__(self):
        self.rows, self.records = {}, {}

    def blob(self, raw):
        return SimpleNamespace(model_dump=lambda **kwargs: dict(sha256=digest(raw.decode())))

    def create_run(self, command, manifest):
        self.saved = manifest
        return manifest

    def manifest(self, run_id):
        return self.saved

    def invocation(self, run_id, name):
        return self.rows.get(name)

    def attempts(self, run_id, name):
        return [self.rows[name]] if name in self.rows else []

    def prepare(self, run_id, name, body, context):
        row = dict(
            run_id=run_id,
            stage=name,
            invocation_id=uuid4(),
            request_record_id=uuid4(),
            attempt_index=0,
            attempt_kind="role",
            status="prepared",
            operation_kind="role",
        )
        self.rows[name] = row
        self.records[row["request_record_id"]] = dict(body=body, context=context)
        return row

    def request(self, row):
        return self.records[row["request_record_id"]]["body"]

    def read(self, ref):
        return self.records[ref.record_id]

    def save(self, value):
        ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        self.records[ref.record_id] = value
        return ref.model_dump(mode="json")


class CaptureGateway:
    def invoke(self, *args, **kwargs):
        raise Captured()


def data_and_profile():
    source = current_materials()
    plan, chosen = selected_plan(source)
    raw = source.packet.model_dump(mode="json")
    for card in raw["dossier"]["knowledge_cards"]:
        if card["card_id"] in chosen:
            card["narrative_excerpt_indices"] = []
    packet = EditorialInput.model_validate(raw)
    materials = EditorialMaterials(packet, source.shared_ref, ())
    extra = next(card.card_id for card in packet.dossier.knowledge_cards if card.card_id not in chosen)
    _, _, _, template, _, reader, fact, plan_review = fixture()
    script = dict(
        utterances=[
            dict(speaker=materials.speaker_labels[0], text="First observation. A second sentence."),
            dict(speaker=materials.speaker_labels[-1], text="The result follows."),
        ]
    )
    anchor = dict(utterance_index=0, quote="First observation.")
    reader["findings"][0]["anchors"] = [anchor]
    fact["findings"][0].update(anchors=[anchor], support_labels=[extra])
    plan_review["findings"][0]["material_refs"] = [next(iter(chosen))]

    def profile(name):
        result = copy(template)
        writer = name == "draft" or name.startswith("revise-")
        result.provider = "anthropic" if writer else "openai"
        result.api_family = "messages" if writer else "responses"
        result.adapter_version = "anthropic-messages-v1" if writer else "openai-responses-v1"
        result.model_revision_or_alias = (
            "claude-opus-5-5"
            if writer
            else "gpt-6.1-sol"
            if name in ("plan", "plan-revise")
            else "gpt-6.1-terra"
        )
        result.output_limit, result.context_limit = 24576, 200000
        result.generation_parameters = dict(
            reasoning=dict(effort="medium"), text=dict(verbosity="high"), store=False, background=False
        )
        return result

    return materials, plan, chosen, extra, script, reader, fact, plan_review, profile


def release(monkeypatch, materials, profile):
    store, journal = ReleaseStore(), Journal()
    for owner in (manifests, stage, reading_stage):
        monkeypatch.setattr(owner, "load_profile", lambda generation, project, ref, name: profile(name))
    ref = ArtifactRef(kind="ModelProfile", artifact_id=uuid4(), revision_id=uuid4())
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=uuid4(),
        actor_id=uuid4(),
        case_id=materials.packet.assignment.key,
        author_ref=materials.shared_ref,
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
        planner_profile_ref=ref,
        plan_reviewer_profile_ref=ref,
        behavior_profile="production/1",
    )
    manifest = manifests.create_manifest(store, store, command, journal, materials)
    return store, journal, command, manifest


def pin_before_reading_audit(monkeypatch):
    """Create an explicit historical /13, /4, /2 release with its original prompt assets."""
    import json
    from dataclasses import replace
    from pathlib import Path

    from podgen.writing.behavior import BehaviorProfile, resolve_behavior

    resources = json.loads(
        (Path(__file__).parent / "fixtures/production-before-reading-audit.json").read_text()
    )
    behavior = replace(
        resolve_behavior("production/1"),
        reader_schema="listener-experience/13",
        resources={**resolve_behavior("narrative-rebuild/1").resources, **resources},
        prompt_source="history",
    )
    monkeypatch.setattr(BehaviorProfile, "reading_audit", property(lambda self: None))
    monkeypatch.setattr(BehaviorProfile, "fact_schema", property(lambda self: "fact-revision/4"))
    monkeypatch.setattr(BehaviorProfile, "verification", property(lambda self: "editorial-recheck/2"))
    from podgen.writing import behavior as behavior_module
    from tests.writing_instruction_history import historical_instructions

    previous_body = behavior_module._body
    monkeypatch.setattr(
        behavior_module,
        "_body",
        lambda entry: (
            historical_instructions(entry["sha256"]) if "fixture" in entry else previous_body(entry)
        ),
    )
    monkeypatch.setattr(manifests, "resolve_behavior", lambda *args, **kwargs: behavior)


def release_before_reading_audit(monkeypatch, materials, profile):
    with monkeypatch.context() as historical:
        pin_before_reading_audit(historical)
        return release(monkeypatch, materials, profile)


def assert_sources(body, materials, selected):
    actual = {card["card_id"]: card for card in payload(body)["support"]}
    refs = preview_references(materials, V9)
    documents = {doc.key: doc for doc in materials.packet.documents}
    aliases = dict(zip(refs.card_ids, refs.aliases, strict=True))
    expected = {
        aliases[card.card_id]: card
        for card in materials.packet.dossier.knowledge_cards
        if card.card_id in selected
    }
    assert actual.keys() == expected.keys()
    for identity, card in expected.items():
        assert actual[identity]["source_excerpts"] == [
            dict(source=documents[excerpt.document].title, text=excerpt.text) for excerpt in card.excerpts
        ]
    return actual
