"""Pinned complete excerpts use the real release, first-draft and addressed-rewrite compilers."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import reading_stage, stage
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.runtime.evaluation.wire_budget import check_visible
from podgen.writing import source_passages
from podgen.writing.behavior import resolve_behavior
from podgen.writing.projection_versions import V8, V9
from podgen.writing.projections import CONTRACT

from tests.revision_cases import composition, scope
from tests.source_excerpt_cases import Captured, CaptureGateway, assert_sources, data_and_profile, release
from tests.test_editorial_independence import payload


def test_complete_saved_excerpts_preserve_duplicates_positions_and_cross_card_coordinates():
    excerpt = SimpleNamespace(document="D", start=0, end=3, text="M1 Cforeign café e\u0301 😀\n" * 3000)
    card = SimpleNamespace(excerpts=[excerpt, excerpt], narrative_excerpt_indices=[1, 0])
    documents = {"D": SimpleNamespace(title="Exact source title")}
    old = source_passages.card_source_passages(card, documents)
    full = source_passages.card_source_passages(card, documents, strategy=source_passages.VERSION)
    assert full == old * 2
    other_card = SimpleNamespace(excerpts=[excerpt], narrative_excerpt_indices=[])
    assert (
        source_passages.card_source_passages(other_card, documents, strategy=source_passages.VERSION) == old
    )
    assert source_passages.card_source_passages(other_card, documents) == []
    assert source_passages.card_source_passages(card, documents, strategy=source_passages.VERSION) == full
    assert (
        source_passages.from_manifest(dict(behavior_profile="production/1", role_projection_version=V9))
        is None
    )
    for name in (
        "existing/1",
        "narrative-rebuild/1",
        "narrative-rebuild/2",
        "editorial-candidate/1",
        "editorial-candidate/2",
    ):
        assert resolve_behavior(name).author_source_excerpts is None
    for version, projection, behavior in [
        ("unknown", V9, "production/1"),
        (source_passages.VERSION, V8, "production/1"),
        (source_passages.VERSION, V9, "existing/1"),
    ]:
        with pytest.raises(ContractError, match="unsupported_author_source_excerpts"):
            source_passages.from_manifest(
                dict(
                    role_projection_version=projection,
                    behavior_profile=behavior,
                    **{source_passages.KEY: version},
                )
            )


def test_actual_release_and_first_draft_keep_all_selected_sources_and_replay_an_unpinned_snapshot(
    monkeypatch,
):
    materials, plan, chosen, _, _, _, _, _, profile = data_and_profile()
    frozen = deepcopy(materials.packet.model_dump(mode="json"))
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    assert manifest[source_passages.KEY] == source_passages.VERSION
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
        stage.execute_stage(
            "draft",
            outputs={},
            records={},
            own_plan=dict(accepted_plan=plan, accepted_plan_ref={}, planning_clearance_ref={}),
            **common,
        )
    live = journal.request(journal.invocation(command.run_id, "draft"))
    snapshot = PreviewSnapshot(manifest, live["instructions"])
    preview = preview_request(
        command.run_id, "draft", profile("draft"), materials, snapshot=snapshot, approved_plan=plan
    )
    assert preview == live
    assert_sources(live, materials, chosen)
    check_visible(live, CONTRACT, max_visible_bytes=manifest["role_input_budget"]["visible_bytes"])
    old = deepcopy(manifest)
    old.pop(source_passages.KEY)
    replay = preview_request(
        command.run_id,
        "draft",
        profile("draft"),
        materials,
        snapshot=PreviewSnapshot(old, live["instructions"]),
        approved_plan=plan,
    )
    previous = payload(replay)
    assert all("source_excerpts" not in card for card in previous["support"])
    current = payload(live)
    for card in current["support"]:
        card.pop("source_excerpts")
    assert current == previous
    assert replay["instructions"] == live["instructions"] and replay["text"] == live["text"]
    assert (
        preview_request(
            command.run_id,
            "draft",
            profile("draft"),
            materials,
            snapshot=PreviewSnapshot(old, live["instructions"]),
            approved_plan=plan,
        )
        == replay
    )
    assert materials.packet.model_dump(mode="json") == frozen


@pytest.mark.parametrize(
    "scale,mode", [("local", "targeted"), ("sequence", "sequence"), ("whole_episode", "structural")]
)
def test_actual_initial_and_later_rewrites_match_preview_and_keep_the_same_fact_union(
    monkeypatch, scale, mode
):
    materials, plan, chosen, extra, script, reader, fact, _, profile = data_and_profile()
    first = dict(utterance_index=0, quote="First observation.")
    last = dict(utterance_index=1, quote="The result follows.")
    reader["revision_scope"] = scope(
        scale,
        anchors=[first],
        **(
            dict(sequence_ranges=[dict(anchors=[first, last], reason="Develop the sequence.")])
            if scale == "sequence"
            else {}
        ),
    )
    fact["revision_scope"] = scope("local", anchors=[first])
    store, journal, command, manifest = release(monkeypatch, materials, profile)
    before = deepcopy((plan, script, reader, fact))
    for number in (1, 2):
        name = f"revise-{number}"
        state = composition("Current") if number == 2 else None
        script_ref = journal.save(dict(output=script, composition=state))
        readings = dict(reader=reader, fact=fact)
        reading_refs = {
            origin: journal.save(dict(output=review, script_ref=script_ref, script_hash=digest(script)))
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
                script_ref=script_ref,
                readings=readings,
                reading_refs=reading_refs,
                decision=combine(readings),
                approved_plan=plan,
            )
        live = journal.request(journal.invocation(command.run_id, name))
        view = payload(live)
        assert view["revision_operation"]["mode"] == mode
        assert_sources(live, materials, chosen | {extra})
        snapshot = PreviewSnapshot(manifest, live["instructions"], script=script, composition=state)
        inputs = dict(
            draft=script, composition=state, reader_feedback=reader, fact_feedback=fact, approved_plan=plan
        )
        assert (
            preview_request(command.run_id, name, profile(name), materials, snapshot=snapshot, **inputs)
            == live
        )
        old = deepcopy(manifest)
        old.pop(source_passages.KEY)
        legacy = preview_request(
            command.run_id,
            name,
            profile(name),
            materials,
            snapshot=PreviewSnapshot(old, live["instructions"], script=script, composition=state),
            **inputs,
        )
        assert {card["card_id"] for card in payload(legacy)["support"]} == {
            card["card_id"] for card in view["support"]
        }
        for card in view["support"]:
            card.pop("source_excerpts", None)
        previous = payload(legacy)
        for card in previous["support"]:
            card.pop("source_excerpts", None)
        for document in (view, previous):
            for task in document.get("revision_tasks", []):
                task.pop("evidence_refs", None)
                task.pop("support_refs", None)
        assert view == previous
        assert live["text"] == legacy["text"] and live["instructions"] == legacy["instructions"]
        check_visible(live, CONTRACT, max_visible_bytes=manifest["role_input_budget"]["visible_bytes"])
    assert (plan, script, reader, fact) == before


def test_planning_cold_reading_and_fact_review_keep_their_previous_payloads(monkeypatch):
    materials, plan, _, _, script, _, _, review, profile = data_and_profile()
    store, _, command, manifest = release(monkeypatch, materials, profile)
    old = deepcopy(manifest)
    old.pop(source_passages.KEY)
    for name in ("plan", "plan-review", "plan-revise", "plan-review-revised", "reader-review", "fact-review"):
        prompt = store.get(
            command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"][name])
        ).payload["template"]
        inputs = (
            dict(candidate=plan)
            if name in ("plan-review", "plan-revise", "plan-review-revised")
            else dict(draft=script)
            if name in ("reader-review", "fact-review")
            else {}
        )
        if name == "plan-revise":
            inputs["plan_review"] = review
        before = preview_request(
            command.run_id, name, profile(name), materials, snapshot=PreviewSnapshot(old, prompt), **inputs
        )
        after = preview_request(
            command.run_id,
            name,
            profile(name),
            materials,
            snapshot=PreviewSnapshot(manifest, prompt),
            **inputs,
        )
        assert before == after
