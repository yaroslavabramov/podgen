from types import SimpleNamespace
from copy import deepcopy
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
    ShowBibleContext,
    SourceCard,
    SourceMap,
    Treatment,
    TreatmentAct,
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
        source_density_target=1,
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
    )

    treatment = treatment_from_plan(
        plan,
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[_source()]),
    )

    assert treatment.acts[0].required_quote_refs == ["quote-1"]
    assert treatment.acts[0].required_evidence_ids == ["evidence-1"]


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


def test_thirty_minute_treatment_accepts_valid_structural_contract() -> None:
    treatment = Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[_valid_act(1), _valid_act(2), _valid_act(3)],
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert gate.passed is True


def test_treatment_rejects_refs_not_owned_by_selected_atom() -> None:
    act = _valid_act(1).model_copy(
        update={"required_evidence_ids": ["other-evidence"], "required_quote_refs": ["other-quote"]}
    )
    treatment = Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[act, _valid_act(2), _valid_act(3)],
    )

    gate = validate_treatment(treatment, [_source()], 1800, research_memo=_memo())

    assert gate.passed is False
    assert any("evidence not attached" in issue for issue in gate.weak_sections)
    assert any("quotes not attached" in issue for issue in gate.weak_sections)


def test_treatment_rejects_stale_quote_ref_even_when_atom_owns_it() -> None:
    treatment = Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[_valid_act(1), _valid_act(2), _valid_act(3)],
    )

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


def test_treatment_model_input_keeps_every_caution_and_constraint() -> None:
    source = _source()
    source.gaps_or_cautions = [
        f"Caution {index}: this source has a specific limitation treatment must preserve."
        for index in range(15)
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
        source_map=SourceMap(source_cards=[source]),
        source_inventory=SourceMap(source_cards=[source]),
        research_audit=None,
        act_count_range=(3, 4),
    )

    assert len(payload["source_cautions"]) == 15
    assert payload["source_cautions"][-1]["note"].startswith("Caution 14:")
    assert len(payload["audit_constraints"]) == 36
    assert payload["audit_constraints"][34].startswith("Risk 34:")
    assert payload["audit_constraints"][-1] == "Open question: One bounded question remains unresolved."


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
    valid = Treatment(
        title="Episode",
        logline="A grounded plan.",
        host_dynamic="A leads; B tests.",
        acts=[_valid_act(1), _valid_act(2), _valid_act(3)],
    )
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
        return Treatment(
            title="Episode",
            logline="A grounded plan.",
            host_dynamic="A leads; B tests.",
            acts=[_valid_act(1), _valid_act(2), _valid_act(3)],
        )

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
