"""The actual preview/compiler boundary uses creative facts and exact short references."""

from types import SimpleNamespace
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.material_names import AliasedNames, display_materials
from podgen.runtime.evaluation.plan_target_recovery import request_candidate
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.role_request import compile_role_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.card_authoring import upgrade_shared
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import CATALOG_KEY, ModelReferences
from podgen.writing.plan_views import candidate_view
from podgen.writing.projection_versions import V4, V5
from tests.own_plan_provider import proposal
from tests.test_card_ids import legacy
from tests.test_editorial_independence import payload


def fixture():
    raw, _ = upgrade_shared(legacy())
    for index, card in enumerate(raw["dossier"]["knowledge_cards"]):
        card["story_facts"] = (
            f"Whole fact {index}: literal M01 and Cforeign, including its original scope.\n" * 100
        )
    packet = EditorialInput.model_validate(raw)
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    references = ModelReferences.create(packet)
    plan = proposal(
        dict(
            material_catalog=materials.material_catalog,
            speaker_labels=[s.label for s in packet.assignment.speakers],
        ),
        "native",
    )
    plan["plan"]["acts"][0]["prerequisite_concepts"] = [
        "Understand the initial observation before the conclusion."
    ]
    profile = SimpleNamespace(
        provider="openai",
        api_family="responses",
        adapter_version="openai-responses-v1",
        allowed_roles=["planner", "plan_reviewer", "writer", "editorial_reviewer"],
        structured_output_mode="json_schema",
        fallback_profile_refs=[],
        output_limit=32768,
        context_limit=1050000,
        model_revision_or_alias="synthetic-pinned-snapshot",
        generation_parameters=dict(
            store=False, background=False, reasoning=dict(effort="high"), text=dict(verbosity="high")
        ),
        timeout_policy=dict(rpc_timeout_seconds=1800, poll_deadline_seconds=1800),
    )
    script = dict(utterances=[dict(speaker=packet.assignment.speakers[0].label, text="Exact spoken words.")])
    finding = dict(
        dimension="factuality",
        priority="important",
        anchors=[dict(utterance_index=0, quote="Exact spoken words.")],
        observation="A specific issue.",
        requested_change="Keep its scope.",
        preserve=["Whole condition."],
    )
    common = dict(
        listener_answer="Listener's own inference.",
        important_limits=[],
        strengths=[],
        findings=[],
        research_requests=[],
        duration_note="Not timed.",
        recommendation="revise",
    )
    reader = dict(common, reading_mode="script_only", findings=[dict(finding, dimension="transition")])
    fact = dict(
        common,
        reading_mode="informed",
        findings=[dict(finding, support_labels=[materials.material_catalog[0]])],
    )
    review = dict(
        decision="revise",
        decision_reason="A specific missing connection.",
        listener_answer="A listener inference.",
        important_limits=[],
        strengths=[],
        findings=[
            dict(
                targets=["/plan/main_question"],
                priority="important",
                observation="A specific gap.",
                requested_change="Connect the observation.",
                preserve=[],
                material_refs=[materials.material_catalog[0]],
            )
        ],
        research_requests=[],
        duration_note="Not timed.",
    )
    return materials, references, plan, profile, script, reader, fact, review


def test_every_v5_role_uses_its_real_creative_or_checker_view_without_losing_questions():
    materials, refs, plan, profile, script, reader, fact, review = fixture()
    frozen = materials.packet.model_dump(mode="json")
    stages = {
        "plan": {},
        "plan-revise": dict(candidate=plan, plan_review=review),
        "plan-review": dict(candidate=plan),
        "plan-review-revised": dict(candidate=plan),
        "draft": dict(approved_plan=plan),
        "revise": dict(approved_plan=plan, draft=script, feedback=dict(reader, reading_mode="informed")),
        "fact-review": dict(draft=script),
        "revise-1": dict(approved_plan=plan, draft=script, reader_feedback=reader, fact_feedback=fact),
        "reader-review": dict(draft=script),
    }
    wires = {
        stage: payload(preview_request(uuid4(), stage, profile, materials, **inputs))
        for stage, inputs in stages.items()
    }
    for stage in ("plan", "plan-revise"):
        assert set(wires[stage]["dossier"]) == {"knowledge_cards"}
        for card, original, alias in zip(
            wires[stage]["dossier"]["knowledge_cards"],
            materials.packet.dossier.knowledge_cards,
            refs.aliases,
            strict=True,
        ):
            assert card == dict(
                claim=original.story_facts,
                sources=list(
                    dict.fromkeys(
                        d.title
                        for q in original.excerpts
                        for d in materials.packet.documents
                        if d.key == q.document
                    )
                ),
                card_id=alias,
            )
        assert wires[stage]["material_catalog"] == list(refs.aliases)
    assert wires["plan-revise"]["revision_guidance"]["findings"][0]["material_refs"] == ["M1"]
    for stage in ("draft", "revise", "revise-1"):
        assert wires[stage]["support"]
        assert all(set(card) == {"claim", "sources"} for card in wires[stage]["support"])
        assert all(
            card["claim"] in {c.story_facts for c in materials.packet.dossier.knowledge_cards}
            for card in wires[stage]["support"]
        )
    assert wires["draft"]["story"]["main_question"] == plan["plan"]["main_question"]
    assert (
        wires["draft"]["story"]["acts"][0]["prerequisite_concepts"]
        == plan["plan"]["acts"][0]["prerequisite_concepts"]
    )
    for stage in ("plan-review", "plan-review-revised"):
        assert wires[stage]["dossier"]["throughline_constraints"] == list(
            materials.packet.dossier.throughline_constraints
        )
        assert wires[stage]["candidate_plan"] == refs.translate(candidate_view(plan, projection_version=V5))
    for cards in (wires["plan-review"]["dossier"]["knowledge_cards"], wires["fact-review"]["support"]):
        for card, original in zip(cards, materials.packet.dossier.knowledge_cards, strict=True):
            assert card["claim"] == original.claim and card["context"] == original.context
            assert card["limitations"] == list(original.limitations)
    assert wires["fact-review"]["necessary_limits"] == list(materials.packet.dossier.throughline_constraints)
    assert set(wires["reader-review"]) == {
        "script",
        "audience",
        "format",
        "genre",
        "output_language",
        "duration_context",
    }
    assert materials.packet.model_dump(mode="json") == frozen


def test_v4_compiler_remains_native_and_pending_repair_candidate_uses_pinned_map():
    materials, refs, plan, profile, _, _, _, _ = fixture()
    body = compile_role_request(
        uuid4(),
        "plan",
        profile,
        materials,
        "Pinned old prompt.",
        response_schema("plan", "editorial-role-packets/1", projection_version=V4),
        projection_version=V4,
    )
    old = payload(body)
    assert old["material_catalog"] == list(materials.material_catalog)
    assert "threads" in old["dossier"]
    assert all("limitations" in c and "story_facts" not in c for c in old["dossier"]["knowledge_cards"])
    assert request_candidate(
        plan, dict(role_input_contract="editorial-role-packets/1", role_projection_version=V4)
    ) == candidate_view(plan, projection_version=V4)
    context = dict(
        role_input_contract="editorial-role-packets/1",
        role_projection_version=V5,
        **{CATALOG_KEY: refs.as_payload()},
    )
    assert request_candidate(plan, context) == refs.translate(candidate_view(plan, projection_version=V5))


def test_human_names_use_aliases_only_to_disambiguate_and_legacy_export_stays_exact():
    refs = ModelReferences("a" * 64, ("Cfirst", "Csecond", "Cthird"))
    original = dict(Cfirst="Same", Csecond="Same", Cthird="Unique")
    assert display_materials(refs.card_ids, AliasedNames(original, refs)) == "Same [M1], Same [M2], Unique"
    assert display_materials(refs.card_ids, original) == "Same [Cfirst], Same [Csecond], Unique"


def test_dedicated_repair_previews_use_the_same_wire_and_preserve_literal_quotes():
    materials, refs, plan, profile, script, _, fact, review = fixture()
    review["findings"][0]["targets"] = ["/missing/field"]
    wire = payload(
        preview_request(uuid4(), "plan-target-repair", profile, materials, candidate=plan, plan_review=review)
    )
    assert wire["candidate_plan"] == refs.translate(candidate_view(plan, projection_version=V5))
    assert wire["invalid_targets"] and wire["allowed_review_targets"]
    fact["findings"][0]["anchors"][0]["utterance_index"] = 999
    repair = preview_request(uuid4(), "anchor-repair", profile, materials, draft=script, feedback=fact)
    assert repair["metadata"]["podgen_stage"] == "fact-review"
    anchor = payload(repair)
    assert set(anchor) == {"invalid_anchors", "selectors"}
    assert anchor["invalid_anchors"] and anchor["selectors"]
