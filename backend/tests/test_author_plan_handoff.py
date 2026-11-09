from copy import deepcopy

import pytest

from podgen.domain.errors import ContractError
from podgen.writing import author_plan
from podgen.writing.projection_versions import V8, V9
from podgen.writing.projections import role_payload
from podgen.writing.reading_views import reading_payload
from tests.revision_cases import composition
from tests.test_v5_role_wire import fixture


def test_approved_editorial_decisions_reach_both_authors_with_resolvable_sources():
    materials, refs, plan, _, script, reader, fact, _ = fixture()
    body = plan["plan"]
    body["duration_strategy"]["assumptions"] = ["Use one encounter to establish the pattern."]
    body["duration_strategy"]["optional_material"] = ["The second group of counts can be cut."]
    body["acts"][0]["promises_opened"] = ["Who used this source?"]
    body["acts"][0]["promises_addressed"] = ["Why look at the water?"]
    frozen = deepcopy(plan)
    args = dict(approved_plan=plan, projection_version=V9, author_plan_handoff=author_plan.VERSION)
    draft = refs.translate(role_payload("draft", materials, **args))
    rewrite_args = dict(
        script=script,
        readings=dict(reader=reader, fact=fact),
        decision=dict(recommendation="revise"),
        **args,
    )
    rewrite = refs.translate(reading_payload("revise", materials, **rewrite_args))
    for packet, field in ((draft, "story"), (rewrite, "editorial_frame")):
        story = packet[field]
        assert story["duration_strategy"] == body["duration_strategy"]
        assert story["ending"] == body["ending"]
        assert story["route_summary"] == body["route_summary"]
        assert story["speaker_contributions"][0]["speaker"] == body["host_functions"][0]["speaker_id"]
        for scene, approved in zip(story["acts"], body["acts"], strict=True):
            for key in ("act_id", "title", "transitions", "promises_opened", "promises_addressed"):
                assert scene[key] == approved[key]
            assert scene["suggested_priority"] == approved["priority"]
        ids = {card["card_id"] for card in packet["support"]}
        for scene in [story["opening"], *story["acts"]]:
            assert scene["material_refs"] and set(scene["material_refs"]) <= ids
            assert all(ref.startswith("M") and ref[1:].isdigit() for ref in scene["material_refs"])
        assert "accepted_plan" not in packet
    assert draft["story"] == rewrite["editorial_frame"]
    changed = composition("Reordered")
    later = refs.translate(reading_payload("revise", materials, composition=changed, **rewrite_args))
    assert later["editorial_frame"]["decisions"] == changed["decisions"]
    assert later["accepted_plan"] == draft["story"]
    cold = reading_payload("reader-review", materials, script=script, **args)
    assert cold == reading_payload("reader-review", materials, script=script, projection_version=V9)
    assert plan == frozen


def test_historical_author_handoff_stays_pinned_and_unknown_versions_fail_closed():
    materials, _, plan, *_ = fixture()
    historical = role_payload("draft", materials, approved_plan=plan, projection_version=V9)
    assert historical["story"]["duration_strategy"].keys() == {"estimated_seconds_range"}
    assert author_plan.from_manifest(dict(role_projection_version=V9)) is None
    for projection, handoff in ((V8, author_plan.VERSION), (V9, "unknown")):
        with pytest.raises(ContractError, match="unsupported_author_plan_handoff"):
            author_plan.from_manifest(dict(role_projection_version=projection, author_plan_handoff=handoff))
