"""Reusable prefixes preserve complete user data and exact historical request layout."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation import planning_stage, script_revision
from podgen.runtime.evaluation.cache_layout import BREAKPOINT
from podgen.runtime.evaluation.compiler import COUNT_FIELDS, response_request
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.wire_budget import check_visible, visible_sizes
from podgen.writing import prompt_cache
from podgen.writing.author_plan import VERSION as PLAN_HANDOFF
from podgen.writing.author_reply import contract_pin, verify_pin
from podgen.writing.behavior import resolve_behavior
from podgen.writing.model_references import preview_references
from podgen.writing.projection_versions import V8, V9
from podgen.writing.reading_views import reading_payload

from tests.revision_cases import composition, scope
from tests.source_excerpt_cases import (
    Captured,
    CaptureGateway,
    data_and_profile,
    release,
    release_before_reading_audit,
)


def parts(body):
    assert [message["role"] for message in body["input"]] == ["user"]
    return body["input"][0]["content"]


def cached_prefix(body):
    blocks = parts(body)
    end = max(i for i, part in enumerate(blocks) if "prompt_cache_breakpoint" in part)
    return blocks[: end + 1]


def test_historical_cold_readings_keep_role_format_and_full_static_prefix(monkeypatch):
    materials, plan, _, _, script, _, _, _, profile = data_and_profile()
    later = deepcopy(script)
    later["utterances"][-1]["text"] += " A new closing thought."
    store, _, command, manifest = release_before_reading_audit(monkeypatch, materials, profile)
    manifest.pop("revision_verification_contract")
    run_id = command.run_id
    for base in ("reader-review", "fact-review"):
        from podgen.domain.contracts.common import ArtifactRef

        manifest["response_schemas"][base + "-2"] = deepcopy(manifest["response_schemas"][base])
        instructions = store.get(
            command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"][base])
        ).payload["template"]
        snapshot = PreviewSnapshot(manifest, instructions)
        first = preview_request(run_id, base, profile(base), materials, snapshot=snapshot, draft=script)
        second = preview_request(
            run_id, base + "-2", profile(base + "-2"), materials, snapshot=snapshot, draft=later
        )
        assert first["instructions"] == second["instructions"]
        assert first["text"]["format"] == second["text"]["format"]
        assert cached_prefix(first) == cached_prefix(second)
        assert first["prompt_cache_options"] == {"mode": "explicit"}
        assert all("script" not in json.loads(part["text"]) for part in cached_prefix(first))
        assert [row["text"] for row in request_payload(second)["script"]["utterances"]] == [
            row["text"] for row in later["utterances"]
        ]
        if base == "fact-review":
            before_script = b"".join(part["text"].encode() for part in cached_prefix(first))
            assert b'"evidence"' in before_script and b'"factual_remit"' in before_script
        assert all(part.get("prompt_cache_breakpoint", BREAKPOINT) == BREAKPOINT for part in parts(first))


def test_plan_reviews_reuse_full_dossier_before_candidates_and_do_not_cache_the_one_off_plan():
    materials, plan, _, _, _, _, _, _, profile = data_and_profile()
    changed = deepcopy(plan)
    changed["plan"]["route_summary"] += " A revised route."
    run_id = uuid4()
    first = preview_request(run_id, "plan-review", profile("plan-review"), materials, candidate=plan)
    second = preview_request(
        run_id, "plan-review-revised", profile("plan-review-revised"), materials, candidate=changed
    )
    assert first["text"]["format"] == second["text"]["format"]
    assert cached_prefix(first) == cached_prefix(second)
    assert any("dossier" in json.loads(part["text"]) for part in cached_prefix(first))
    assert all("candidate_plan" not in json.loads(part["text"]) for part in cached_prefix(first))
    assert (
        request_payload(second)["candidate_plan"]["plan"]["route_summary"] == changed["plan"]["route_summary"]
    )
    once = preview_request(run_id, "plan", profile("plan"), materials)
    assert once["prompt_cache_options"] == {"mode": "explicit"}
    assert not any("prompt_cache_breakpoint" in part for part in parts(once))


def test_rewrite_prefix_keeps_the_accepted_plan_and_original_cards_when_new_fact_support_is_added():
    materials, plan, chosen, extra, script, reader, fact, _, profile = data_and_profile()
    first_anchor = dict(utterance_index=0, quote="First observation.")
    reader["revision_scope"] = scope("whole_episode", anchors=[first_anchor])
    fact["revision_scope"] = scope("local", anchors=[first_anchor])
    initial_fact = deepcopy(fact)
    for finding in initial_fact["findings"]:
        finding["support_labels"] = [next(iter(chosen))]
    run_id = uuid4()
    common = dict(draft=script, approved_plan=plan, reader_feedback=reader)
    first = preview_request(
        run_id, "revise-1", profile("revise-1"), materials, fact_feedback=initial_fact, **common
    )
    second = preview_request(
        run_id,
        "revise-2",
        profile("revise-2"),
        materials,
        fact_feedback=fact,
        composition=composition("Latest"),
        **common,
    )
    assert first["instructions"] == second["instructions"]
    assert first["text"]["format"] == second["text"]["format"]
    assert cached_prefix(first) == cached_prefix(second)
    a, b = request_payload(first), request_payload(second)
    assert a["accepted_plan"] == b["accepted_plan"]
    assert a["editorial_frame"]["main_question"] == a["accepted_plan"]["main_question"]
    assert b["editorial_frame"]["main_question"] == "Latest question"
    assert "prompt_cache_options" not in first
    assert all("editorial_frame" not in json.loads(part["text"]) for part in cached_prefix(first))
    refs = preview_references(materials, V9)
    aliases = dict(zip(refs.card_ids, refs.aliases, strict=True))
    assert {card["card_id"] for card in a["support"]} == {aliases[c] for c in chosen}
    assert {card["card_id"] for card in b["support"]} == {aliases[c] for c in chosen | {extra}}
    cached_cards = [
        card for part in cached_prefix(second) for card in json.loads(part["text"]).get("support", [])
    ]
    assert aliases[extra] not in {card["card_id"] for card in cached_cards}
    assert json.loads(parts(second)[-1]["text"])["additional_support"][0]["card_id"] == aliases[extra]
    assert "additional_support" not in a and "additional_support" not in b
    old = reading_payload(
        "revise",
        materials,
        script=script,
        readings=dict(reader=reader, fact=initial_fact),
        decision=dict(recommendation="revise"),
        approved_plan=plan,
        projection_version=V9,
        author_plan_handoff=PLAN_HANDOFF,
    )
    # Every field of the old initial frame survives, with the same permissions, in the stable plan.
    assert a["accepted_plan"] == refs.translate(old["editorial_frame"])


def test_draft_is_lossless_without_an_unreusable_anthropic_cache_write():
    materials, plan, _, _, _, _, _, _, profile = data_and_profile()
    body = preview_request(uuid4(), "draft", profile("draft"), materials, approved_plan=plan)
    assert len(parts(body)) > 1
    assert not any("prompt_cache_breakpoint" in part for part in parts(body))
    assert "prompt_cache_options" not in body
    assert request_payload(body)["story"]["ending"] == plan["plan"]["ending"]


def test_all_parts_are_visible_to_budget_and_counting_and_duplicates_fail_explicitly():
    _, _, _, _, _, _, _, _, profile = data_and_profile()
    semantic = dict(
        output_language="ru",
        assignment=dict(question="Exact brief"),
        script=dict(text=" Exact speech\n😀" * 3000),
    )
    body = response_request(
        uuid4(),
        "reader-review",
        profile("reader-review"),
        semantic,
        "Exact instructions",
        dict(type="object"),
        prompt_cache_layout=prompt_cache.VERSION,
    )
    assert request_payload(body) == semantic
    count = {key: value for key, value in body.items() if key in COUNT_FIELDS}
    assert count["input"] == body["input"] and count["prompt_cache_options"] == {"mode": "explicit"}
    size = visible_sizes(body)["total"]
    with pytest.raises(ContractError, match="role_visible_byte_budget_exceeded"):
        check_visible(body, "editorial-role-packets/1", max_visible_bytes=size - 1)
    check_visible(body, "editorial-role-packets/1", max_visible_bytes=size)
    bad = deepcopy(body)
    bad["input"][0]["content"].append(dict(type="input_text", text='{"output_language":"en"}'))
    with pytest.raises(ContractError, match="duplicate_multipart_payload_field"):
        request_payload(bad)
    bad = deepcopy(body)
    bad["metadata"]["podgen_prompt_cache_layout"] = "unknown/1"
    with pytest.raises(ContractError, match="unsupported_prompt_cache_layout"):
        request_payload(bad)


def test_missing_pin_preserves_exact_single_part_wire_and_other_profiles_remain_unpinned():
    _, _, _, _, _, _, _, _, profile = data_and_profile()
    semantic = dict(output_language="ru", script=dict(text="Exact text\n"))
    body = response_request(
        uuid4(), "reader-review-2", profile("reader-review-2"), semantic, "Exact instructions", {}
    )
    assert parts(body) == [
        dict(
            type="input_text",
            text=json.dumps(semantic, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        )
    ]
    assert body["text"]["format"]["name"] == "reader_review_2"
    assert request_payload(body) == semantic and "prompt_cache_options" not in body
    assert (
        prompt_cache.from_manifest(dict(behavior_profile="production/1", role_projection_version=V9)) is None
    )
    for name in (
        "existing/1",
        "narrative-rebuild/1",
        "narrative-rebuild/2",
        "editorial-candidate/1",
        "editorial-candidate/2",
    ):
        assert resolve_behavior(name).prompt_cache_layout is None
    for value, projection, behavior in (
        ("unknown", V9, "production/1"),
        (prompt_cache.VERSION, V8, "production/1"),
        (prompt_cache.VERSION, V9, "existing/1"),
    ):
        with pytest.raises(ContractError, match="unsupported_prompt_cache_layout"):
            prompt_cache.from_manifest(
                dict(
                    behavior_profile=behavior, role_projection_version=projection, **{prompt_cache.KEY: value}
                )
            )


def test_actual_plan_review_stage_matches_preview_for_both_candidates(monkeypatch):
    materials, plan, _, _, _, _, _, _, profile = data_and_profile()
    store, journal, command, saved = release(monkeypatch, materials, profile)
    assert saved[prompt_cache.KEY] == prompt_cache.VERSION
    monkeypatch.setattr(planning_stage, "load_profile", lambda generation, project, ref, name: profile(name))
    for name in ("plan-review", "plan-review-revised"):
        with pytest.raises(Captured):
            planning_stage.execute_planning_stage(
                name,
                journal=journal,
                gateway=CaptureGateway(),
                generation=store,
                command=command,
                manifest=saved,
                materials=materials,
                totals=dict(input=0, output=0),
                guard=lambda: None,
                candidate=plan,
            )
        live = journal.request(journal.invocation(command.run_id, name))
        assert (
            preview_request(
                command.run_id,
                name,
                profile(name),
                materials,
                snapshot=PreviewSnapshot(saved, live["instructions"]),
                candidate=plan,
            )
            == live
        )


def test_old_production_snapshot_keeps_single_part_requests_initial_frame_and_bound_ids(monkeypatch):
    materials, plan, _, _, script, reader, fact, _, profile = data_and_profile()
    first = dict(utterance_index=0, quote="First observation.")
    reader["revision_scope"] = scope("none")
    reader.update(recommendation="keep", findings=[])
    fact["revision_scope"] = scope("local", anchors=[first])
    _, _, command, saved = release(monkeypatch, materials, profile)
    saved.pop(prompt_cache.KEY)
    saved.pop("revision_context_contract", None)
    saved["revision_response_schemas"] = script_revision.schemas()
    saved["writer_response_contract"] = contract_pin(saved, materials.speaker_labels)
    script_revision.validate_manifest(saved)
    verify_pin(saved, materials.speaker_labels)
    instructions = resolve_behavior("production/1").instructions("dual-revise", materials.format).body
    before = deepcopy(saved)
    inputs = dict(draft=script, approved_plan=plan, reader_feedback=reader, fact_feedback=fact)
    body = preview_request(
        command.run_id,
        "revise-1",
        profile("revise-1"),
        materials,
        snapshot=PreviewSnapshot(saved, instructions),
        **inputs,
    )
    assert len(parts(body)) == 1 and "prompt_cache_options" not in body
    assert "accepted_plan" not in request_payload(body)
    assert "ending" in request_payload(body)["editorial_frame"]
    assert body["text"]["format"]["name"] == "revise_1"
    assert "enum" in body["text"]["format"]["schema"]["$defs"]["ReplaceText"]["properties"]["target_id"]
    assert (
        preview_request(
            command.run_id,
            "revise-1",
            profile("revise-1"),
            materials,
            snapshot=PreviewSnapshot(saved, instructions),
            **inputs,
        )
        == body
    )
    assert saved == before


@pytest.mark.parametrize(
    "cards",
    [
        [],
        [dict(card_id="M2", text=" Exact extra\n😀 ")],
        [dict(card_id="M1", text=" Base "), dict(card_id="M2", text=" Extra ")],
    ],
)
def test_empty_and_additional_support_roundtrip_without_omission_or_ambiguous_duplicate_keys(cards):
    _, _, _, _, _, _, _, _, profile = data_and_profile()
    semantic = dict(accepted_plan=dict(opening=dict(material_refs=["M1"])), support=cards)
    body = response_request(
        uuid4(),
        "revise-1",
        profile("revise-1"),
        semantic,
        "Exact instructions",
        {},
        prompt_cache_layout=prompt_cache.VERSION,
    )
    assert request_payload(body) == semantic
    stable = next(json.loads(part["text"]) for part in parts(body) if "support" in json.loads(part["text"]))
    assert stable["support"] == [card for card in cards if card["card_id"] == "M1"]
    body["input"][0]["content"].append(dict(type="input_text", text='{"support":[]}'))
    with pytest.raises(ContractError, match="duplicate_multipart_payload_field"):
        request_payload(body)
