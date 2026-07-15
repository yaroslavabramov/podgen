from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2.agents.treatment_context import treatment_input
from app.generation_v2.agents.treatment_plan_conversion import treatment_from_plan
from app.generation_v2.pipeline.validation import validate_treatment
from app.generation_v2.prompting.system import BASE_PODCAST_SYSTEM, TREATMENT_SYSTEM
from app.generation_v2.research_workspace_memo import research_memo_from_workspace
from app.generation_v2.schemas import (
    EditorialAtom,
    ResearchAudit,
    ResearchMemo,
    QuoteCard,
    ShowBibleContext,
    SourceAssessment,
    SourceCard,
    SourceGapRecord,
    SourceMap,
    Treatment,
    TreatmentAct,
    TreatmentAtomAnchor,
    TreatmentAtomDisposition,
    TreatmentPlan,
    TreatmentPlanAct,
)


def _source(source_id: str = "source-1") -> SourceCard:
    return SourceCard(
        source_id=source_id,
        title="Source",
        source_type="web",
        summary="Grounded source summary.",
        coverage_status="ready",
    )


def _memo(*, claim: str = "The central assertion.") -> ResearchMemo:
    return ResearchMemo(
        title="Memo",
        thesis="Supported answer.",
        research_synthesis="Internal digest.",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                claim=claim,
                explanation="The evidence explains the mechanism.",
                source_ids=["source-1"],
                quote_refs=["quote-1"],
                evidence_ids=["evidence-1"],
                story_value="This gives the episode a concrete turn.",
                source_coverage_status="ready",
                story_weight="core",
            )
        ],
    )


def _valid_act(number: int) -> TreatmentAct:
    return TreatmentAct(
        act_number=number,
        title=f"Act {number}",
        story_role="mechanism",
        opening_question="How does it work?",
        main_turn="The mechanism becomes clear.",
        ending_turn="The next consequence follows.",
        source_focus=["source-1"],
        target_minutes=10,
        required_atom_refs=["A01"],
        required_quote_refs=["quote-1"],
        required_evidence_ids=["evidence-1"],
        episode_supported_answer="Supported answer.",
        atom_anchors=[
            TreatmentAtomAnchor(
                atom_ref="A01",
                kind="mechanism",
                title="Mechanism",
                claim="The central assertion.",
                story_value="This gives the episode a concrete turn.",
                story_weight="core",
            )
        ],
        source_density_target=1,
    )


def _valid_treatment(*, acts: list[TreatmentAct] | None = None) -> Treatment:
    return Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        supported_answer="Supported answer.",
        acts=acts if acts is not None else [_valid_act(1), _valid_act(2), _valid_act(3)],
        core_atom_dispositions=[
            TreatmentAtomDisposition(atom_ref="A01", disposition="used", act_number=1)
        ],
    )


def test_workspace_claim_survives_memo_handoff() -> None:
    memo = research_memo_from_workspace(
        SimpleNamespace(config={"user_prompt": "Topic"}),
        {
            "atom_drafts": [
                {
                    "atom_id": "atom-1",
                    "kind": "mechanism",
                    "title": "Mechanism",
                    "claim": "Central claim that must not be replaced by its explanation.",
                    "explanation": "Longer explanation.",
                    "story_value": "Useful material.",
                    "source_ids": ["source-1"],
                    "evidence_ids": ["evidence-1"],
                }
            ]
        },
    )

    assert memo.editorial_atoms[0].claim == "Central claim that must not be replaced by its explanation."
    assert memo.editorial_atoms[0].explanation == "Longer explanation."


def test_treatment_conversion_carries_atom_quote_refs() -> None:
    memo = _memo()
    plan = TreatmentPlan(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[
            TreatmentPlanAct(
                act_number=1,
                title="Mechanism",
                story_role="mechanism",
                opening_question="How?",
                main_turn="The mechanism turns.",
                ending_turn="The consequence follows.",
                source_focus=["S01"],
                target_minutes=30,
                required_atom_refs=["A01"],
                source_density_target=1,
            )
        ],
        core_atom_dispositions=[
            TreatmentAtomDisposition(atom_ref="A01", disposition="used", act_number=1)
        ],
    )

    treatment = treatment_from_plan(
        plan,
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
    )

    assert treatment.acts[0].required_quote_refs == ["quote-1"]
    assert treatment.acts[0].required_evidence_ids == ["evidence-1"]
    assert treatment.supported_answer == memo.thesis
    assert treatment.acts[0].episode_supported_answer == memo.thesis
    assert treatment.acts[0].atom_anchors[0].claim == memo.editorial_atoms[0].claim
    assert treatment.core_atom_dispositions[0].atom_ref == "A01"


def test_treatment_conversion_does_not_hide_unknown_source_handle() -> None:
    plan = TreatmentPlan(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[
            TreatmentPlanAct(
                act_number=index,
                title=f"Act {index}",
                story_role="mechanism",
                opening_question="How?",
                main_turn="The mechanism turns.",
                ending_turn="The consequence follows.",
                source_focus=["S99"],
                target_minutes=10,
                required_atom_refs=["A01"],
                source_density_target=1,
            )
            for index in range(1, 4)
        ],
    )
    treatment = treatment_from_plan(
        plan,
        research_memo=_memo(),
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert "S99" in treatment.acts[0].source_focus
    assert gate.passed is False
    assert any("unknown or unavailable sources" in issue for issue in gate.weak_sections)


def test_thirty_minute_treatment_rejects_mechanically_empty_acts() -> None:
    malformed = Treatment(
        title="",
        logline="",
        host_dynamic="",
        acts=[
            TreatmentAct(
                act_number=99,
                title="",
                story_role="",
                opening_question="",
                main_turn="",
                ending_turn="",
                source_focus=["source-1"],
                target_minutes=10,
                source_density_target=1,
            )
            for _ in range(3)
        ],
    )

    gate = validate_treatment(malformed, [_source()], 1800, research_memo=_memo())

    assert gate.passed is False
    assert any("unique and sequential" in issue for issue in gate.weak_sections)
    assert any("must not be empty" in issue for issue in gate.weak_sections)
    assert any("lacks required editorial atom anchors" in issue for issue in gate.weak_sections)


def test_treatment_rejects_empty_act_list() -> None:
    treatment = Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[],
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert gate.passed is False
    assert "Treatment must contain at least one act." in gate.weak_sections


def test_thirty_minute_treatment_accepts_valid_structural_contract() -> None:
    treatment = _valid_treatment()

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert gate.passed is True


def test_treatment_rejects_refs_not_owned_by_selected_atom() -> None:
    act = _valid_act(1).model_copy(
        update={"required_evidence_ids": ["other-evidence"], "required_quote_refs": ["other-quote"]}
    )
    treatment = _valid_treatment(acts=[act, _valid_act(2), _valid_act(3)])

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert gate.passed is False
    assert any("evidence not attached" in issue for issue in gate.weak_sections)
    assert any("quotes not attached" in issue for issue in gate.weak_sections)


def test_treatment_rejects_stale_quote_ref_even_when_atom_owns_it() -> None:
    treatment = _valid_treatment()

    gate = validate_treatment(
        treatment,
        [_source()],
        1800,
        research_memo=_memo(),
        valid_evidence_ids={"evidence-1"},
        valid_quote_refs=set(),
    )

    assert gate.passed is False
    assert any("not exact evidence in the current run" in issue for issue in gate.weak_sections)


def test_treatment_model_input_keeps_full_validated_research_and_repair_fields() -> None:
    claim = "x" * 900
    topic = "t" * 5_000
    repair_instructions = [f"Violation {index}: correct the mechanical contract." for index in range(30)]
    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": topic, "duration_target": 1800, "language": "en"}),
        research_memo=_memo(claim=claim),
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
        research_audit=None,
        act_count_range=(3, 4),
        repair_instructions=repair_instructions,
    )

    atom = payload["research_atoms"][0]
    assert atom["claim"] == claim
    assert atom["explanation"] == "The evidence explains the mechanism."
    assert "claim_preview" not in atom
    assert payload["episode_brief"]["topic"] == topic
    assert payload["repair_instructions"] == repair_instructions
    assert "untrusted data" in BASE_PODCAST_SYSTEM
    assert "untrusted data" in TREATMENT_SYSTEM


def test_treatment_model_input_exposes_selected_quote_without_backend_ids() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    research_evidence_id = str(uuid.uuid4())
    source = _source(source_id)
    source.source_version_ids = [source_version_id]
    memo = ResearchMemo(
        title="Memo",
        thesis="Supported answer.",
        research_synthesis="Internal digest.",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                claim="The mechanism changed.",
                explanation="The evidence explains the mechanism.",
                source_ids=[source_id],
                quote_refs=[evidence_id, research_evidence_id],
                evidence_ids=[evidence_id, research_evidence_id],
                story_value="This provides the turn.",
                story_weight="core",
            )
        ],
    )
    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=memo,
        source_map=SourceMap(
            quote_cards=[
                QuoteCard(
                    source_id=source_id,
                    source_version_id=source_version_id,
                    evidence_id=evidence_id,
                    quote="The mechanism changed in 2024.",
                    locator=f"[source_id={source_id} page_marker=4 paragraph=2] section {source_version_id}",
                    narrative_use=f"Use {evidence_id} for the central turn.",
                )
            ]
        ),
        source_inventory=SourceMap(source_cards=[source]),
        research_audit=None,
        act_count_range=(3, 4),
        evidence_records=[
            {
                "id": evidence_id,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "snippet_text": "The mechanism changed in 2024.",
                "locator_text": "section 4",
                "metadata": {},
            },
            {
                "id": research_evidence_id,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "snippet_text": "A second exact line with context.",
                "locator_text": f"[source_id={source_id} page_marker=5 paragraph=1] section 5",
                "metadata": {"quote_text": "A second exact line."},
            },
        ],
    )

    assert payload["research_atoms"][0]["selected_quote_refs"] == ["Q01", "Q02"]
    assert payload["selected_quotes"][0]["quote_ref"] == "Q01"
    assert payload["selected_quotes"][0]["exact_quote"] == "The mechanism changed in 2024."
    assert payload["selected_quotes"][1]["quote_ref"] == "Q02"
    assert payload["selected_quotes"][1]["exact_quote"] == "A second exact line."
    serialized = json.dumps(payload)
    assert source_id not in serialized
    assert source_version_id not in serialized
    assert evidence_id not in serialized
    assert research_evidence_id not in serialized
    assert "page_marker" not in serialized


def test_treatment_model_input_keeps_complete_semantic_handoff_without_reader() -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    long_quote = "Exact source wording " * 180
    long_thesis = "Supported answer detail " * 90
    long_source_summary = "Source role detail " * 160
    source_coverage = [
        "Ready: the central mechanism is supported.",
        "Partial: " + "bounded detail " * 50,
        "Deferred gap: the downstream impact could not be established.",
    ]
    source = _source(source_id)
    source.summary = long_source_summary
    memo = ResearchMemo(
        title="Memo",
        thesis=long_thesis,
        research_synthesis="Internal digest.",
        source_coverage=source_coverage,
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                claim="The mechanism changed.",
                explanation="The evidence explains the mechanism.",
                source_ids=[source_id],
                quote_refs=[evidence_id],
                evidence_ids=[evidence_id],
                story_value="This provides the turn.",
                story_weight="core",
            )
        ],
    )

    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=memo,
        source_map=SourceMap(
            quote_cards=[
                QuoteCard(
                    source_id=source_id,
                    evidence_id=evidence_id,
                    quote=long_quote,
                    locator="section 4",
                    narrative_use="Central turn.",
                )
            ]
        ),
        source_inventory=SourceMap(source_cards=[source]),
        research_audit=None,
        act_count_range=(3, 4),
    )

    assert payload["episode_brief"]["supported_answer"] == long_thesis
    assert "supported_answer_preview" not in payload["episode_brief"]
    assert payload["selected_quotes"][0]["exact_quote"] == long_quote
    assert "exact_quote_preview" not in payload["selected_quotes"][0]
    assert payload["source_context"][0]["role_hint"] == long_source_summary
    assert "role_hint_preview" not in payload["source_context"][0]
    assert payload["source_coverage"] == source_coverage
    assert "source_coverage_previews" not in payload


def test_treatment_source_coverage_replaces_only_known_internal_evidence_ids() -> None:
    job_only_evidence_id = str(uuid.uuid4())
    source_material_uuid = str(uuid.uuid4())
    coverage = (
        f"Deferred evidence {job_only_evidence_id} remains unresolved; "
        f"the source itself prints identifier {source_material_uuid}."
    )
    memo = _memo().model_copy(update={"source_coverage": [coverage]})

    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
        research_audit=None,
        act_count_range=(3, 4),
        evidence_records=[{"id": job_only_evidence_id}],
    )

    rendered = payload["source_coverage"][0]
    assert job_only_evidence_id not in rendered
    assert "Deferred evidence E001 remains unresolved" in rendered
    assert source_material_uuid in rendered


def test_treatment_model_input_keeps_every_caution_and_constraint() -> None:
    inventory_source = _source()
    inventory_source.gaps_or_cautions = ["Provider indexing is incomplete."]
    editorial_source = _source()
    editorial_source.gaps_or_cautions = [
        f"Caution {index}: this source has a specific limitation treatment must preserve." for index in range(15)
    ]
    memo = _memo().model_copy(
        update={
            "risk_register": [f"Risk {index}: retain this qualification." for index in range(35)],
            "open_questions": ["One bounded question remains unresolved."],
        }
    )
    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=memo,
        source_map=SourceMap(source_cards=[editorial_source]),
        source_inventory=SourceMap(source_cards=[inventory_source]),
        research_audit=None,
        act_count_range=(3, 4),
    )

    assert len(payload["source_cautions"]) == 15
    assert payload["source_cautions"][-1]["note"].startswith("Caution 14:")
    assert len(payload["audit_constraints"]) == 36
    assert payload["audit_constraints"][34].startswith("Risk 34:")
    assert payload["audit_constraints"][-1] == "Open question: One bounded question remains unresolved."


def test_treatment_uses_structured_gap_lineage_without_prose_source_refs() -> None:
    source = _source()
    unrelated_source = _source("source-2")
    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=_memo(),
        source_map=SourceMap(
            source_cards=[source],
            gaps=["The institutional accounts still conflict."],
            gap_records=[
                SourceGapRecord(
                    gap_id="gap:scoped",
                    kind="contradiction",
                    description="The institutional accounts still conflict.",
                    source_ids=[source.source_id],
                ),
                SourceGapRecord(
                    gap_id="gap:global",
                    kind="gap",
                    description="No selected source establishes the downstream impact.",
                ),
                SourceGapRecord(
                    gap_id="gap:unrelated",
                    kind="caution",
                    description="An unrelated source has a separate limitation.",
                    source_ids=[unrelated_source.source_id],
                ),
            ],
        ),
        source_inventory=SourceMap(source_cards=[source, unrelated_source]),
        research_audit=None,
        act_count_range=(3, 4),
    )

    assert payload["source_cautions"] == [
        {
            "category": "corpus_contradiction",
            "source_refs": ["S01"],
            "note": "The institutional accounts still conflict.",
        },
        {
            "category": "corpus_gap",
            "source_refs": [],
            "note": "No selected source establishes the downstream impact.",
        },
    ]


def test_treatment_input_does_not_turn_positive_audit_verdict_into_story_material() -> None:
    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=_memo(),
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
        research_audit=ResearchAudit(
            passed=True,
            score=1.0,
            verdict="All checks passed; no constraints remain.",
        ),
        act_count_range=(3, 4),
    )

    assert payload["audit_constraints"] == []
    assert "All checks passed" not in json.dumps(payload)


def test_treatment_model_input_filters_inventory_noise_but_keeps_editorial_limitations() -> None:
    inventory_source = _source()
    inventory_source.gaps_or_cautions = ["OCR errors likely.", "Provider indexing is incomplete."]
    editorial_source = _source()
    editorial_source.gaps_or_cautions = ["OCR errors likely.", "Quote carefully."]

    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=_memo(),
        source_map=SourceMap(
            source_cards=[editorial_source],
            source_assessments=[
                SourceAssessment(
                    assessment_id="assessment-1",
                    source_id="source-1",
                    representation_status="partial",
                    representation_reason="Tables require the native document view.",
                    limitations=["The publication date is not independently corroborated."],
                )
            ],
        ),
        source_inventory=SourceMap(source_cards=[inventory_source]),
        research_audit=None,
        act_count_range=(3, 4),
    )

    assert [item["note"] for item in payload["source_cautions"]] == [
        "Quote carefully.",
        "The publication date is not independently corroborated.",
        "Tables require the native document view.",
    ]


def test_treatment_model_input_does_not_drop_short_semantic_atom_caution() -> None:
    memo = _memo()
    memo.editorial_atoms[0].risk = "Timing remains uncertain."

    payload = treatment_input(
        run=SimpleNamespace(config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"}),
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
        research_audit=None,
        act_count_range=(3, 4),
    )

    assert payload["research_atoms"][0]["caution"] == "Timing remains uncertain."


def test_treatment_allows_an_explicit_core_omission_with_reason() -> None:
    memo = _memo().model_copy(
        update={
            "editorial_atoms": [
                _memo().editorial_atoms[0],
                EditorialAtom(
                    kind="context",
                    title="Necessary context",
                    claim="The context establishes the baseline.",
                    explanation="The context establishes the baseline.",
                    source_ids=["source-1"],
                    evidence_ids=["evidence-2"],
                    story_value="The listener needs this baseline.",
                    story_weight="supporting",
                    source_coverage_status="ready",
                ),
            ]
        }
    )
    act = TreatmentAct(
        act_number=1,
        title="Context",
        story_role="context",
        opening_question="What is the baseline?",
        main_turn="The baseline becomes clear.",
        ending_turn="The mechanism remains outside this episode's scope.",
        source_focus=["source-1"],
        target_minutes=30,
        required_atom_refs=["A02"],
        required_evidence_ids=["evidence-2"],
        episode_supported_answer=memo.thesis,
        atom_anchors=[
            TreatmentAtomAnchor(
                atom_ref="A02",
                kind="context",
                title="Necessary context",
                claim="The context establishes the baseline.",
                story_value="The listener needs this baseline.",
                story_weight="supporting",
            )
        ],
        source_density_target=1,
    )
    treatment = Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        supported_answer=memo.thesis,
        acts=[act],
        core_atom_dispositions=[
            TreatmentAtomDisposition(
                atom_ref="A01",
                disposition="omitted",
                reason="The mechanism needs a second independent source before it can carry an act.",
            )
        ],
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=memo)

    assert gate.passed is True


def test_treatment_rejects_a_core_atom_marked_omitted_but_used() -> None:
    treatment = _valid_treatment().model_copy(
        update={
            "core_atom_dispositions": [
                TreatmentAtomDisposition(
                    atom_ref="A01",
                    disposition="omitted",
                    reason="No place in the episode.",
                )
            ]
        }
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert gate.passed is False
    assert any("marked omitted but is used" in issue for issue in gate.weak_sections)


def test_treatment_requires_exactly_one_disposition_for_each_core_atom() -> None:
    missing = _valid_treatment().model_copy(update={"core_atom_dispositions": []})
    duplicate = _valid_treatment().model_copy(
        update={
            "core_atom_dispositions": [
                TreatmentAtomDisposition(atom_ref="A01", disposition="used", act_number=1),
                TreatmentAtomDisposition(atom_ref="A01", disposition="used", act_number=2),
            ]
        }
    )

    missing_gate = validate_treatment(missing, [_source()], 1800, research_memo=_memo())
    duplicate_gate = validate_treatment(duplicate, [_source()], 1800, research_memo=_memo())

    assert any("lack an explicit disposition" in issue for issue in missing_gate.weak_sections)
    assert any("duplicate dispositions" in issue for issue in duplicate_gate.weak_sections)


def test_treatment_rejects_an_act_number_on_an_omitted_core_atom() -> None:
    treatment = _valid_treatment().model_copy(
        update={
            "acts": [],
            "core_atom_dispositions": [
                TreatmentAtomDisposition(
                    atom_ref="A01",
                    disposition="omitted",
                    act_number=1,
                    reason="The core material is deliberately outside this episode.",
                )
            ],
        }
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert any("must not carry an act_number" in issue for issue in gate.weak_sections)


def test_treatment_rejects_an_atom_anchor_that_changes_the_research_claim() -> None:
    fabricated_anchor = _valid_act(1).atom_anchors[0].model_copy(update={"claim": "A different claim."})
    act = _valid_act(1).model_copy(update={"atom_anchors": [fabricated_anchor]})

    gate = validate_treatment(
        _valid_treatment(acts=[act]),
        [_source()],
        1800,
        research_memo=_memo(),
    )

    assert gate.passed is False
    assert any("does not preserve the research atom" in issue for issue in gate.weak_sections)


@pytest.mark.asyncio
async def test_treatment_mechanical_failure_gets_one_targeted_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.pipeline import editorial_research_stage as stage_module
    from app.generation_v2 import research_workspace_persistence
    from app.generation_v2.research_workspace_repair_budget import (
        REPAIR_COMPLETED,
        TREATMENT_REWRITE_BUDGET,
    )

    malformed = Treatment(
        title="",
        logline="",
        host_dynamic="",
        acts=[
            TreatmentAct(
                act_number=99,
                title="",
                story_role="",
                opening_question="",
                main_turn="",
                ending_turn="",
                source_focus=["source-1"],
                target_minutes=10,
                source_density_target=1,
            )
            for _ in range(3)
        ],
    )
    valid = _valid_treatment()
    repair_inputs: list[list[str] | None] = []
    gate_saves: list[str] = []
    stored_workspace: dict = {"repair_budget": {}, "repair_operations": {}}

    async def fake_start_phase(*_args, **_kwargs) -> None:
        return None

    async def fake_build_treatment(*_args, repair_instructions=None, **_kwargs) -> Treatment:
        repair_inputs.append(repair_instructions)
        return malformed if len(repair_inputs) == 1 else valid

    async def fake_save_treatment(*_args, **_kwargs) -> str:
        return f"treatment-{len(repair_inputs)}"

    async def fake_save_gate(*_args, treatment_artifact_id: str, **_kwargs) -> str:
        gate_saves.append(treatment_artifact_id)
        return "gate-1"

    async def fake_load_workspace(_db, _run_id):
        return deepcopy(stored_workspace)

    async def fake_save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        stored_workspace.clear()
        stored_workspace.update(deepcopy(workspace))
        return SimpleNamespace(payload=workspace)

    async def fake_commit() -> None:
        return None

    monkeypatch.setattr(stage_module, "start_phase", fake_start_phase)
    monkeypatch.setattr(stage_module, "build_treatment", fake_build_treatment)
    monkeypatch.setattr(stage_module, "save_treatment_artifact", fake_save_treatment)
    monkeypatch.setattr(stage_module, "save_treatment_gate_artifact", fake_save_gate)
    monkeypatch.setattr(research_workspace_persistence, "load_research_workspace", fake_load_workspace)
    monkeypatch.setattr(research_workspace_persistence, "save_research_workspace", fake_save_workspace)

    runner = stage_module.EditorialResearchStageRunner.__new__(stage_module.EditorialResearchStageRunner)
    runner.db = SimpleNamespace(commit=fake_commit)
    runner.run = SimpleNamespace(id=uuid.uuid4(), config={"duration_target": 1800})
    runner.runtime = SimpleNamespace()
    runner.triage_state = SimpleNamespace(source_inventory=SourceMap(source_cards=[_source()]))
    runner.show_bible = ShowBibleContext()
    runner.values = {
        "source_map": SourceMap(),
        "run_source_cards": [_source()],
        "evidence_tools": [],
        "corpus": SimpleNamespace(vector_store_id=None),
        "source_map_artifact_id": "source-map",
        "source_artifact_ids": [],
    }

    result = await runner.run_treatment(
        SimpleNamespace(memo=_memo(), memo_artifact_id="memo", atom_artifact_ids=[]),
        ResearchAudit(passed=True, score=1.0, verdict="ready"),
        "audit",
    )

    assert result == (valid, "treatment-2")
    assert repair_inputs[0] is None
    assert repair_inputs[1]
    assert any("unique and sequential" in issue for issue in repair_inputs[1] or [])
    assert gate_saves == ["treatment-1"]
    assert stored_workspace["repair_operations"][TREATMENT_REWRITE_BUDGET]["status"] == REPAIR_COMPLETED
    assert stored_workspace["repair_budget"][TREATMENT_REWRITE_BUDGET] == 1


@pytest.mark.asyncio
async def test_treatment_rewrite_resumes_persisted_in_progress_instructions_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace_persistence
    from app.generation_v2.pipeline import editorial_research_stage as stage_module
    from app.generation_v2.research_workspace_repair_budget import (
        REPAIR_COMPLETED,
        REPAIR_IN_PROGRESS,
        TREATMENT_REWRITE_BUDGET,
    )

    stored_workspace: dict = {
        "repair_budget": {},
        "repair_operations": {
            TREATMENT_REWRITE_BUDGET: {
                "kind": "treatment_rewrite",
                "status": REPAIR_IN_PROGRESS,
                "payload": {"repair_instructions": ["Renumber every act and fill required fields."]},
            }
        },
    }
    repair_inputs: list[list[str] | None] = []

    async def fake_load_workspace(_db, _run_id):
        return deepcopy(stored_workspace)

    async def fake_save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        stored_workspace.clear()
        stored_workspace.update(deepcopy(workspace))
        return SimpleNamespace(payload=workspace)

    async def fake_start_phase(*_args, **_kwargs) -> None:
        return None

    async def fake_build_treatment(*_args, repair_instructions=None, **_kwargs) -> Treatment:
        repair_inputs.append(repair_instructions)
        return _valid_treatment()

    async def fake_save_treatment(*_args, **_kwargs) -> str:
        return "treatment-resumed"

    async def fake_commit() -> None:
        return None

    monkeypatch.setattr(research_workspace_persistence, "load_research_workspace", fake_load_workspace)
    monkeypatch.setattr(research_workspace_persistence, "save_research_workspace", fake_save_workspace)
    monkeypatch.setattr(stage_module, "start_phase", fake_start_phase)
    monkeypatch.setattr(stage_module, "build_treatment", fake_build_treatment)
    monkeypatch.setattr(stage_module, "save_treatment_artifact", fake_save_treatment)

    runner = stage_module.EditorialResearchStageRunner.__new__(stage_module.EditorialResearchStageRunner)
    runner.db = SimpleNamespace(commit=fake_commit)
    runner.run = SimpleNamespace(id=uuid.uuid4(), config={"duration_target": 1800})
    runner.runtime = SimpleNamespace()
    runner.triage_state = SimpleNamespace(source_inventory=SourceMap(source_cards=[_source()]))
    runner.show_bible = ShowBibleContext()
    runner.values = {
        "source_map": SourceMap(),
        "run_source_cards": [_source()],
        "evidence_tools": [],
        "corpus": SimpleNamespace(vector_store_id=None),
        "source_map_artifact_id": "source-map",
        "source_artifact_ids": [],
    }

    result = await runner.run_treatment(
        SimpleNamespace(memo=_memo(), memo_artifact_id="memo", atom_artifact_ids=[]),
        ResearchAudit(passed=True, score=1.0, verdict="ready"),
        "audit",
    )

    assert result and result[1] == "treatment-resumed"
    assert repair_inputs == [["Renumber every act and fill required fields."]]
    assert stored_workspace["repair_operations"][TREATMENT_REWRITE_BUDGET]["status"] == REPAIR_COMPLETED
    assert stored_workspace["repair_budget"][TREATMENT_REWRITE_BUDGET] == 1
