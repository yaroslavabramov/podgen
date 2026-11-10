"""All ending roles pin new instructions without changing their material or edit permissions."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from podgen.domain.contracts.common import ArtifactRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.writing.behavior import resolve_behavior
from podgen.writing.editorial_contract import EditorialInput
from tests.revision_cases import composition, scope
from tests.source_excerpt_cases import data_and_profile, release
from tests.test_planning_v9 import selected

ASSETS = Path(__file__).parents[1] / "src/podgen/writing/behavior/assets"
PREVIOUS = {
    "plan": "933d42194b42f45af89c54c294fd0d66f1c70001eeea33513895de2bf519a265",
    "plan-revise": "55f6064cf4ab5dd8359562c0aca328d94dd44911cbccdb530676b1f28173c408",
    "plan-review-solo": "f65d60d0bd4cd5c29e2e61d357ea5bbe25369eeed5a4967929f8ab077f772375",
    "plan-review-dialogue": "d79bad884366548c474ac500fa739f61b0c92b6e33e2ca28dec8fb2947186f09",
    "draft-solo": "13b21c996662ca29534c6ed2b0c2ac1ffd6ca324b25c1390be95e908c939059a",
    "draft-dialogue": "cf8863c91cf3139c8d5e51317240ea3417f98dcd807ce7a8d78686048ae1dd5a",
    "reader-review": "5fa00d503635ada0f568e75726a40e308a8616480d7bd4503e2f2f5624d80002",
    "dual-revise": "31bcb5fcd4c5443a23340af4a0653a5a1b922d70d3662e37801465892f10bfdf",
    "fact-review": "62b7066f83b9aa7b486fc1e5825b56c01ae1725d5d2ad449252e3c2b18cdd4f2",
}


def compiled_requests(monkeypatch, format):
    materials, source, _, _, script, reader, fact, review, profile = data_and_profile()
    candidate = selected(source)
    if format == "dialogue":
        raw = materials.packet.model_dump(mode="json")
        raw["assignment"]["format"] = format
        raw["assignment"]["speakers"].append(
            dict(
                label="СОБЕСЕДНИК", function="Ask about the investigation.", speech_register="Conversational."
            )
        )
        materials = EditorialMaterials(EditorialInput.model_validate(raw), materials.shared_ref, ())
        host = deepcopy(candidate["plan"]["host_functions"][0])
        host["speaker_id"] = "СОБЕСЕДНИК"
        candidate["plan"]["host_functions"].append(host)
        script["utterances"][-1]["speaker"] = "СОБЕСЕДНИК"
    anchor = dict(utterance_index=0, quote="First observation.")
    for reading in (reader, fact):
        reading["revision_scope"] = scope("local", anchors=[anchor])
    store, _, command, manifest = release(monkeypatch, materials, profile)
    cases = {
        "plan": {},
        "plan-revise": dict(candidate=candidate, plan_review=review),
        "plan-review": dict(candidate=candidate),
        "plan-review-revised": dict(candidate=candidate),
        "draft": dict(approved_plan=candidate),
        "reader-review": dict(draft=script),
        "fact-review": dict(draft=script),
    }
    for number in (1, 2):
        cases[f"revise-{number}"] = dict(
            draft=script,
            approved_plan=candidate,
            reader_feedback=reader,
            fact_feedback=fact,
            composition=composition("Current") if number == 2 else None,
        )
    for name, inputs in cases.items():
        role = (
            "dual-revise"
            if name.startswith("revise-")
            else "plan-review"
            if name.startswith("plan-review")
            else name
        )
        key = f"{role}-{format}" if role in ("draft", "plan-review") else role
        previous = (ASSETS / f"{PREVIOUS[key]}.md").read_text()
        assert hashlib.sha256(previous.encode()).hexdigest() == PREVIOUS[key]
        pinned = store.get(
            command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"][name])
        ).payload["template"]
        assert pinned == resolve_behavior().instructions(role, format).body
        requests = []
        for instructions in (previous, pinned):
            snapshot = PreviewSnapshot(manifest, instructions)
            requests.append(
                preview_request(command.run_id, name, profile(name), materials, snapshot=snapshot, **inputs)
            )
        yield name, requests[0], requests[1]


@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_all_ending_role_requests_pin_changes_and_preserve_every_other_request_field(monkeypatch, format):
    for name, previous, current in compiled_requests(monkeypatch, format):
        if name == "fact-review":
            assert current == previous
        else:
            assert current["instructions"] != previous["instructions"]
            for obsolete in (
                "repetition of the main point",
                "ordinary summing-up",
                "well-chosen simple summary",
            ):
                assert obsolete not in current["instructions"]
            current["instructions"] = previous["instructions"]
            assert current == previous


def test_revision_inherits_one_complete_planning_task_and_craft_reference_stays_immutable():
    behavior = resolve_behavior()
    plan = behavior.instructions("plan", "solo").body
    revision = behavior.instructions("plan-revise", "solo").body
    assert revision.startswith(plan) and revision.count(plan) == 1
    assert behavior.instructions("plan", "dialogue").body == plan
    assert behavior.instructions("plan-revise", "dialogue").body == revision
    assert behavior.craft("ru") == resolve_behavior("narrative-rebuild/1").craft("ru")
