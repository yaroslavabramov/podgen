from __future__ import annotations

import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.audit_context import (
    audit_workspace_page_tool,
    normalize_research_audit,
    research_audit_input,
)
from app.generation_v2.agents.audit_evidence_context import (
    audit_evidence_catalog,
    audit_evidence_previews,
    audit_tools_with_evidence_refs,
)
from app.generation_v2.pipeline.research_repair import enqueue_audit_research_repair_jobs
from app.generation_v2.pipeline.research_validation import validate_research_memo_evidence
from app.generation_v2.research_workspace import build_research_workspace_tools
from app.generation_v2.research_workspace_job_state import prepare_workspace_jobs_for_resume
from app.generation_v2.research_workspace_prompt_payloads import research_workspace_summary_for_prompt
from app.generation_v2.research_workspace_prompt_payloads import research_job_prompt_payload
from app.generation_v2.research_workspace_repair_budget import (
    AUDIT_RERUN_BUDGET,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_RESERVED,
    TARGETED_REPAIR_BUDGET,
    TREATMENT_REWRITE_BUDGET,
    complete_persisted_repair_operation,
    repair_budget,
    reserve_repair_operation_in_workspace,
    start_persisted_repair_operation,
)
from app.generation_v2.research_source_map_prompting import (
    source_map_research_page,
    source_map_research_summary,
)
from app.generation_v2.runtime_checkpoint_restart import checkpoint_restart_instructions
from app.generation_v2.runtime_contracts import default_checkpoint_policy
from app.generation_v2.runtime_tool_output import model_facing_tool_output
from app.generation_v2.schemas import (
    EditorialAtom,
    ResearchAudit,
    ResearchAuditIssue,
    ResearchMemo,
    QuoteCard,
    SourceCard,
    SourceCluster,
    SourceMap,
    SourceRelationship,
    SourceTriageReport,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context
from app.generation_v2.source_triage_workspace import build_workspace_tools
from app.generation_v2.source_triage_workspace_prompting import (
    source_set_prompt_payload,
    workspace_summary_for_prompt,
)
from app.generation_v2.tools.evidence_core_utils import exact_quote_text_matches
from app.generation_v2.tools.evidence_model_read_persistence import EvidenceModelReadPersistenceMixin
from app.models.generation_v2 import SourceRepresentationKind


def _source(source_id: str | None = None, *, title: str = "Source") -> SourceCard:
    return SourceCard(
        source_id=source_id or str(uuid.uuid4()),
        title=title,
        source_type="pdf",
        summary="Full source summary.",
        coverage_status="ready",
    )


def test_contradictory_research_audit_cannot_pass() -> None:
    audit = normalize_research_audit(
        ResearchAudit(
            passed=True,
            score=0.9,
            verdict="pass",
            issues=[
                ResearchAuditIssue(
                    severity="major",
                    target_step="editorial_research",
                    description="Grounding must be repaired.",
                )
            ],
        )
    )

    assert audit.passed is False
    assert audit.blocking is True
    assert audit.repair_targets == ["editorial_research"]


def test_audit_with_missing_material_cannot_pass_without_structured_issue() -> None:
    audit = normalize_research_audit(
        ResearchAudit(
            passed=True,
            score=0.9,
            verdict="pass",
            missing_material=["A required source is still missing."],
        )
    )

    assert audit.passed is False
    assert audit.blocking is True


@pytest.mark.asyncio
async def test_repair_budget_survives_runner_recreation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.pipeline.editorial_research_stage import EditorialResearchStageRunner
    from app.generation_v2 import research_workspace_persistence

    run_id = uuid.uuid4()
    stored_workspace: dict = {"repair_budget": {}}

    async def load_workspace(_db, _run_id):
        return {**stored_workspace, "repair_budget": dict(stored_workspace.get("repair_budget") or {})}

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        stored_workspace.clear()
        stored_workspace.update(workspace)
        return SimpleNamespace(payload=workspace)

    async def commit() -> None:
        return None

    monkeypatch.setattr(research_workspace_persistence, "load_research_workspace", load_workspace)
    monkeypatch.setattr(research_workspace_persistence, "save_research_workspace", save_workspace)

    audit = ResearchAudit(
        passed=False,
        score=0.2,
        verdict="Audit must rerun.",
        issues=[
            ResearchAuditIssue(
                severity="blocking",
                target_step="research_audit",
                description="Required evidence was not read.",
            )
        ],
    )
    first_runner = EditorialResearchStageRunner.__new__(EditorialResearchStageRunner)
    first_runner.db = SimpleNamespace(commit=commit)
    first_runner.run = SimpleNamespace(id=run_id)
    second_runner = EditorialResearchStageRunner.__new__(EditorialResearchStageRunner)
    second_runner.db = first_runner.db
    second_runner.run = first_runner.run

    assert await first_runner.handle_failed_audit(audit) == "rerun_audit"
    assert stored_workspace["repair_operations"][AUDIT_RERUN_BUDGET]["status"] == REPAIR_RESERVED
    assert repair_budget(stored_workspace)[AUDIT_RERUN_BUDGET] == 0
    assert await second_runner.handle_failed_audit(audit) == "rerun_audit"
    assert await start_persisted_repair_operation(
        second_runner.db,
        generation_run_id=run_id,
        budget_name=AUDIT_RERUN_BUDGET,
    )
    assert stored_workspace["repair_operations"][AUDIT_RERUN_BUDGET]["status"] == REPAIR_IN_PROGRESS
    assert repair_budget(stored_workspace)[AUDIT_RERUN_BUDGET] == 0
    assert await complete_persisted_repair_operation(
        second_runner.db,
        generation_run_id=run_id,
        budget_name=AUDIT_RERUN_BUDGET,
    )
    with pytest.raises(RuntimeError, match="Audit must rerun"):
        await second_runner.handle_failed_audit(audit)

    assert stored_workspace["repair_budget"][AUDIT_RERUN_BUDGET] == 1
    assert stored_workspace["repair_operations"][AUDIT_RERUN_BUDGET]["status"] == REPAIR_COMPLETED


@pytest.mark.asyncio
async def test_reserved_audit_rerun_is_completed_by_resumed_model_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace_persistence
    from app.generation_v2.pipeline import editorial_research_stage as stage_module

    stored_workspace = {
        "repair_budget": {},
        "repair_operations": {
            AUDIT_RERUN_BUDGET: {
                "kind": "research_audit_rerun",
                "status": REPAIR_RESERVED,
                "payload": {"audit": {"verdict": "Retry the audit."}},
            }
        },
    }
    commits = 0

    async def load_workspace(_db, _run_id):
        return deepcopy(stored_workspace)

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        stored_workspace.clear()
        stored_workspace.update(deepcopy(workspace))
        return SimpleNamespace(payload=workspace)

    async def start_phase(*_args, **_kwargs) -> None:
        return None

    async def audit_memo(*_args, **_kwargs) -> ResearchAudit:
        return ResearchAudit(passed=True, score=1.0, verdict="Audit rerun passed.")

    async def save_audit(*_args, **_kwargs) -> str:
        return "audit-resumed"

    async def commit() -> None:
        nonlocal commits
        commits += 1

    monkeypatch.setattr(research_workspace_persistence, "load_research_workspace", load_workspace)
    monkeypatch.setattr(research_workspace_persistence, "save_research_workspace", save_workspace)
    monkeypatch.setattr(stage_module, "start_phase", start_phase)
    monkeypatch.setattr(stage_module, "audit_research_memo", audit_memo)
    monkeypatch.setattr(stage_module, "save_main_research_audit_artifact", save_audit)

    runner = stage_module.EditorialResearchStageRunner.__new__(stage_module.EditorialResearchStageRunner)
    runner.db = SimpleNamespace(commit=commit)
    runner.run = SimpleNamespace(id=uuid.uuid4())
    runner.runtime = SimpleNamespace()
    runner.triage_state = SimpleNamespace(source_inventory=SourceMap())
    runner.show_bible = SimpleNamespace()
    runner.values = {"source_map": SourceMap(), "evidence_tools": [], "corpus": SimpleNamespace(vector_store_id=None)}
    memo_pass = SimpleNamespace(
        memo=ResearchMemo(
            title="Memo",
            thesis="Thesis",
            research_synthesis="Synthesis",
            editorial_atoms=[],
        ),
        memo_artifact_id="memo",
        atom_artifact_ids=[],
        memo_gate_artifact_id="memo-gate",
    )

    audit, artifact_id = await runner.run_audit(memo_pass)

    assert audit.passed is True
    assert artifact_id == "audit-resumed"
    assert stored_workspace["repair_operations"][AUDIT_RERUN_BUDGET]["status"] == REPAIR_COMPLETED
    assert stored_workspace["repair_budget"][AUDIT_RERUN_BUDGET] == 1
    assert commits == 2


@pytest.mark.asyncio
async def test_memo_gate_without_open_questions_enqueues_durable_research_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_module
    from app.generation_v2.pipeline.editorial_research_stage import EditorialResearchStageRunner

    run_id = uuid.uuid4()
    stored_workspace: dict = {
        "source_order": ["S01"],
        "investigation_jobs": [],
        "research_gaps": [],
        "memo_fields": {"title": "Invalid memo"},
    }

    async def load_workspace(_db, _run_id):
        return deepcopy(stored_workspace)

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        snapshot = deepcopy(workspace)
        stored_workspace.clear()
        stored_workspace.update(snapshot)
        return SimpleNamespace(payload=workspace)

    async def commit() -> None:
        return None

    monkeypatch.setattr(workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_research_workspace", save_workspace)

    runner = EditorialResearchStageRunner.__new__(EditorialResearchStageRunner)
    runner.db = SimpleNamespace(commit=commit)
    runner.run = SimpleNamespace(id=run_id)
    gate = ResearchAudit(
        passed=False,
        score=0.2,
        verdict="Core atom is not grounded.",
        issues=[
            ResearchAuditIssue(
                severity="blocking",
                target_step="editorial_research",
                description="Core atom references missing evidence.",
                suggested_repair="Find and attach current-run evidence.",
            )
        ],
    )
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
        editorial_atoms=[],
        open_questions=[],
    )

    assert await runner.handle_failed_memo_gate(memo, gate) is True
    assert stored_workspace["research_gaps"][0]["question"] == "Core atom references missing evidence."
    assert stored_workspace["investigation_jobs"][0]["status"] == "planned"
    operation = stored_workspace["repair_operations"][TARGETED_REPAIR_BUDGET]
    assert operation["status"] == REPAIR_RESERVED
    assert operation["payload"]["repair_job_ids"] == [
        stored_workspace["investigation_jobs"][0]["job_id"]
    ]
    assert repair_budget(stored_workspace)[TARGETED_REPAIR_BUDGET] == 0


@pytest.mark.asyncio
async def test_in_progress_research_repair_survives_new_corpus_attempt_with_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_module

    source = _source()
    inventory = SourceMap(source_cards=[source])
    source_map = SourceMap(source_cards=[source])
    existing = {
        "schema_version": "research_workspace.v2",
        "source_order": ["S01"],
        "source_map_signature": {"old": True},
        "corpus_revision": 1,
        "attempt": 1,
        "repair_budget": {},
        "repair_operations": {
            TARGETED_REPAIR_BUDGET: {
                "kind": "research_jobs",
                "status": REPAIR_IN_PROGRESS,
                "payload": {
                    "repair_job_ids": ["repair-job"],
                    "resume_jobs": [
                        {
                            "job_id": "repair-job",
                            "label": "Repair grounding",
                            "questions": ["Which evidence grounds the claim?"],
                            "source_refs": ["S01"],
                            "status": "in_progress",
                            "observations": [{"note": "Old attempt", "evidence_ids": ["old-evidence"]}],
                            "evidence_ids": ["old-evidence"],
                            "question_outcomes": [],
                        }
                    ],
                    "research_gaps": [
                        {
                            "gap_id": "gap-1",
                            "question": "Which evidence grounds the claim?",
                            "severity": "blocking",
                            "status": "open",
                            "source_refs": ["S01"],
                            "evidence_ids": ["old-evidence"],
                        }
                    ],
                    "audit_repair_feedback": [
                        {
                            "target_step": "editorial_research",
                            "severity": "blocking",
                            "description": "Ground the core claim.",
                        }
                    ],
                },
            },
            TREATMENT_REWRITE_BUDGET: {
                "kind": "treatment_rewrite",
                "status": REPAIR_RESERVED,
                "payload": {"repair_instructions": ["Renumber every act."]},
            },
        },
    }
    saved: dict = {}

    async def load_workspace(_db, _run_id):
        return existing

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        saved.update(workspace)
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_research_workspace", save_workspace)

    workspace = await workspace_module.ensure_research_workspace(
        None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        source_map=source_map,
        source_inventory=inventory,
        run_view=SimpleNamespace(revision=1, attempt=2),
    )

    assert workspace["attempt"] == 2
    assert workspace["status"] == "repair_planned"
    assert workspace["repair_budget"] == {
        TARGETED_REPAIR_BUDGET: 0,
        AUDIT_RERUN_BUDGET: 0,
        TREATMENT_REWRITE_BUDGET: 0,
    }
    repair_job = workspace["investigation_jobs"][0]
    assert repair_job["job_id"] == "repair-job"
    assert repair_job["status"] == "planned"
    assert repair_job["observations"] == []
    assert repair_job["evidence_ids"] == []
    assert workspace["audit_repair_feedback"][0]["description"] == "Ground the core claim."
    assert workspace["research_gaps"][0]["evidence_ids"] == []
    assert workspace["repair_operations"][TARGETED_REPAIR_BUDGET]["status"] == REPAIR_IN_PROGRESS
    assert workspace["repair_operations"][TREATMENT_REWRITE_BUDGET]["payload"]["repair_instructions"] == [
        "Renumber every act."
    ]
    assert saved == workspace


@pytest.mark.asyncio
async def test_reserved_finalizer_repair_survives_new_corpus_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_module

    source = _source()
    inventory = SourceMap(source_cards=[source])
    existing = {
        "schema_version": "research_workspace.v2",
        "source_order": ["S01"],
        "source_map_signature": {"old": True},
        "corpus_revision": 1,
        "attempt": 1,
        "repair_budget": {},
        "repair_operations": {
            TARGETED_REPAIR_BUDGET: {
                "kind": "research_finalizer",
                "status": REPAIR_RESERVED,
                "payload": {
                    "resume_jobs": [
                        {
                            "job_id": "base-job",
                            "questions": ["What changed?"],
                            "source_refs": ["S01"],
                            "status": "mapped",
                            "evidence_ids": ["old-evidence"],
                            "question_outcomes": [
                                {"question": "What changed?", "status": "answered", "note": "Old answer"}
                            ],
                        }
                    ],
                    "audit_repair_feedback": [
                        {
                            "target_step": "research_finalizer",
                            "severity": "blocking",
                            "description": "Preserve the main thesis in the memo.",
                        }
                    ],
                },
            }
        },
    }

    async def load_workspace(_db, _run_id):
        return existing

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_research_workspace", save_workspace)

    workspace = await workspace_module.ensure_research_workspace(
        None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        source_map=inventory,
        source_inventory=inventory,
        run_view=SimpleNamespace(revision=1, attempt=2),
    )

    assert workspace["investigation_jobs"][0]["status"] == "planned"
    assert workspace["investigation_jobs"][0]["evidence_ids"] == []
    assert workspace["audit_repair_feedback"][0]["description"] == "Preserve the main thesis in the memo."
    assert workspace["repair_operations"][TARGETED_REPAIR_BUDGET]["status"] == REPAIR_RESERVED
    assert repair_budget(workspace)[TARGETED_REPAIR_BUDGET] == 0


@pytest.mark.asyncio
async def test_research_finalizer_repair_completes_budget_only_after_durable_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.agents import research_memo_agent

    source = _source()
    evidence_id = str(uuid.uuid4())
    run = SimpleNamespace(id=uuid.uuid4(), config={"user_prompt": "Topic"})
    stored_workspace = {
        "status": "finalizer_repair_needed",
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "base-job",
                "questions": ["What changed?"],
                "source_refs": ["S01"],
                "status": "mapped",
                "question_outcomes": [
                    {
                        "question": "What changed?",
                        "status": "answered",
                        "note": "The source records the change.",
                    }
                ],
            }
        ],
        "atom_drafts": [
            {
                "atom_id": "atom-1",
                "kind": "change",
                "title": "Change",
                "claim": "The system changed.",
                "explanation": "The source records the change.",
                "story_value": "Creates the turn.",
                "source_ids": [source.source_id],
                "source_refs": ["S01"],
                "evidence_ids": [evidence_id],
            }
        ],
        "memo_fields": {},
        "repair_budget": {},
        "repair_operations": {
            TARGETED_REPAIR_BUDGET: {
                "kind": "research_finalizer",
                "status": REPAIR_RESERVED,
                "payload": {"audit_repair_feedback": [{"description": "Repair the thesis."}]},
            }
        },
    }
    commits = 0

    async def ensure_workspace(*_args, **_kwargs):
        return deepcopy(stored_workspace)

    async def load_workspace(_db, _run_id):
        return deepcopy(stored_workspace)

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        stored_workspace.clear()
        stored_workspace.update(deepcopy(workspace))
        return SimpleNamespace(payload=workspace)

    async def finalize_agent(**_kwargs):
        stored_workspace["memo_fields"] = {
            "title": "Memo",
            "thesis": "The system changed.",
            "research_synthesis": "The source records the change.",
            "source_coverage": ["S01 covered"],
        }
        return SimpleNamespace(ready=True, summary="finalized", notes=[])

    async def commit() -> None:
        nonlocal commits
        commits += 1

    monkeypatch.setattr(research_memo_agent, "ensure_research_workspace", ensure_workspace)
    monkeypatch.setattr(research_memo_agent, "load_research_workspace", load_workspace)
    monkeypatch.setattr(research_memo_agent, "save_research_workspace", save_workspace)
    monkeypatch.setattr(research_memo_agent, "build_research_workspace_tools", lambda **_kwargs: [])
    monkeypatch.setattr(research_memo_agent, "build_research_finalizer_agent", finalize_agent)

    memo = await research_memo_agent.build_research_memo(
        SimpleNamespace(commit=commit),  # type: ignore[arg-type]
        run,
        SimpleNamespace(),  # type: ignore[arg-type]
        source_cards=[source],
        quote_cards=[],
        source_map=SourceMap(source_cards=[source]),
        source_inventory=SourceMap(source_cards=[source]),
        run_planner=False,
    )

    assert memo.thesis == "The system changed."
    assert stored_workspace["repair_operations"][TARGETED_REPAIR_BUDGET]["status"] == REPAIR_COMPLETED
    assert stored_workspace["repair_budget"][TARGETED_REPAIR_BUDGET] == 1
    assert commits == 2
    assert reserve_repair_operation_in_workspace(
        stored_workspace,
        TARGETED_REPAIR_BUDGET,
        operation_kind="research_finalizer",
    ) is None


@pytest.mark.asyncio
async def test_in_progress_triage_repair_survives_new_corpus_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_module

    source = _source()
    inventory = SourceMap(source_cards=[source])
    existing = {
        "schema_version": "source_triage_workspace.v1",
        "manifest_hash": "manifest",
        "corpus_revision": 1,
        "attempt": 1,
        "source_order": ["S01"],
        "repair_operation": {
            "status": "in_progress",
            "payload": {
                "gate": {"passed": False, "summary": "Missing grounding"},
                "completion": {"ready": False, "summary": "Initial gate failed", "notes": []},
                "repair_set_ids": ["repair_1"],
                "repair_sets": [
                    {
                        "set_id": "repair_1",
                        "label": "Repair grounding",
                        "source_refs": ["S01"],
                        "status": "in_progress",
                        "repair": True,
                        "evidence_ids": ["old-evidence"],
                        "written_actions": [{"evidence_ids": ["old-evidence"]}],
                        "observations": [{"evidence_ids": ["old-evidence"]}],
                    }
                ],
            },
        },
    }
    saved: dict = {}

    async def load_workspace(_db, _run_id):
        return existing

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        saved.update(workspace)
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_module, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_source_triage_workspace", save_workspace)

    workspace = await workspace_module.ensure_source_triage_workspace(
        None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=SimpleNamespace(manifest_hash="manifest", revision=1, attempt=2),
        inventory=inventory,
    )

    assert workspace["attempt"] == 2
    assert workspace["status"] == "repair_planned"
    assert workspace["repair_operation"]["status"] == "in_progress"
    assert workspace["source_sets"][0]["set_id"] == "repair_1"
    assert workspace["source_sets"][0]["status"] == "planned"
    assert workspace["source_sets"][0]["evidence_ids"] == []
    assert workspace["source_sets"][0]["written_actions"] == []
    assert saved == workspace


def test_research_memo_rejects_nonexact_and_stale_quote_evidence() -> None:
    source_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    quote_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Supported answer.",
        research_synthesis="Synthesis.",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                claim="A claim.",
                explanation="A grounded mechanism.",
                source_ids=[source_id],
                evidence_ids=[quote_id],
                quote_refs=[quote_id],
                story_value="Explains the mechanism.",
            )
        ],
    )
    run_view = SimpleNamespace(
        selected_source_ids=[source_id],
        selected_source_version_ids=[version_id],
        revision=2,
        attempt=1,
    )
    evidence = {
        "id": quote_id,
        "source_id": source_id,
        "source_version_id": version_id,
        "corpus_revision": 2,
        "attempt": 1,
        "evidence_strength": "snippet",
        "metadata": {"usage_status": "selected"},
    }

    nonexact = validate_research_memo_evidence(
        memo,
        evidence_records=[evidence],
        run_view=run_view,
        duration_seconds=1800,
    )
    stale = validate_research_memo_evidence(
        memo,
        evidence_records=[{**evidence, "evidence_strength": "exact", "corpus_revision": 1}],
        run_view=run_view,
        duration_seconds=1800,
    )
    unverified_model_quote = validate_research_memo_evidence(
        memo,
        evidence_records=[
            {
                **evidence,
                "evidence_strength": "exact",
                "metadata": {"usage_status": "selected", "model_file_read": True},
            }
        ],
        run_view=run_view,
        duration_seconds=1800,
    )
    valid = validate_research_memo_evidence(
        memo,
        evidence_records=[{**evidence, "evidence_strength": "exact"}],
        run_view=run_view,
        duration_seconds=1800,
    )
    fabricated_memo = memo.model_copy(deep=True)
    fabricated_memo.editorial_atoms[0].quote_refs = ["invented-quote-ref"]
    fabricated = validate_research_memo_evidence(
        fabricated_memo,
        evidence_records=[{**evidence, "evidence_strength": "exact"}],
        run_view=run_view,
        duration_seconds=1800,
    )

    assert nonexact.passed is False
    assert "not canonically verified as exact" in " ".join(nonexact.missing_material)
    assert stale.passed is False
    assert "outside the current run corpus view" in " ".join(stale.missing_material)
    assert unverified_model_quote.passed is False
    assert "not canonically verified as exact" in " ".join(unverified_model_quote.missing_material)
    assert valid.passed is True
    assert fabricated.passed is False
    assert "unknown quote evidence id" in " ".join(fabricated.missing_material)


@pytest.mark.parametrize(
    ("agent_name", "phase"),
    [
        ("source_triage_planner_agent", "v2_source_triage"),
        ("source_triage_set_agent_set_1", "v2_source_triage"),
        ("source_triage_finalizer_agent", "v2_source_triage"),
        ("research_planner_agent", "v2_editorial_research"),
        ("research_job_agent_job_1", "v2_editorial_research"),
        ("research_finalizer_agent", "v2_editorial_research"),
    ],
)
def test_active_triage_and_research_agents_use_checkpoints(agent_name: str, phase: str) -> None:
    assert default_checkpoint_policy(agent_name, phase=phase).enabled is True


def test_audit_and_wrong_phase_agents_do_not_use_editorial_checkpoints() -> None:
    assert default_checkpoint_policy("research_auditor", phase="v2_research_audit").enabled is False
    assert default_checkpoint_policy("research_job_agent_job_1", phase="v2_research_audit").enabled is False


def test_checkpoint_restart_instructions_match_the_active_phase() -> None:
    triage = checkpoint_restart_instructions(
        {"phase": "v2_source_triage", "agent_name": "source_triage_set_agent_set_1"}
    )
    research = checkpoint_restart_instructions(
        {"phase": "v2_editorial_research", "agent_name": "research_job_agent_job_1"}
    )

    assert "upsert_source_card" in triage
    assert "finish_research_job" in research
    assert "upsert_source_card" not in research


@pytest.mark.asyncio
async def test_triage_checkpoint_snapshot_keeps_current_set_and_page_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import runtime_checkpoint_snapshot, source_triage_workspace

    source_sets = [
        {
            "set_id": f"set_{idx}",
            "label": f"Set {idx}",
            "status": "planned",
            "source_refs": [f"S{idx:02d}"],
        }
        for idx in range(43)
    ]
    current = source_sets[-1]
    current.update(
        {
            "status": "in_progress",
            "purpose": "Map the current source.",
            "observations": [
                {
                    "source_refs": ["S42"],
                    "evidence_ids": [f"ev-{idx}"],
                    "note": f"Observation {idx} " + ("x" * 700),
                    "pending_questions": [],
                }
                for idx in range(14)
            ],
            "written_actions": [],
        }
    )
    workspace = {
        "status": "mapping",
        "source_order": [f"S{idx:02d}" for idx in range(43)],
        "source_sets": source_sets,
        "current_set": {"set_id": "set_42", "started_at": "2026-07-14T12:00:00"},
        "source_capsules": {
            "S42": {"source_ref": "S42", "title": "Current source", "readable": True}
        },
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(source_triage_workspace, "load_source_triage_workspace", load)
    state: dict = {}
    await runtime_checkpoint_snapshot._add_source_triage_workspace_state(
        None,  # type: ignore[arg-type]
        state,
        generation_run_id=uuid.uuid4(),
    )

    payload = state["source_triage_workspace"]
    assert payload["source_sets_page"] == {
        "total_count": 43,
        "returned_count": 40,
        "omitted_count": 3,
        "cursor": "0",
        "next_cursor": "40",
    }
    assert payload["current_set"]["set_id"] == "set_42"
    assert payload["current_set"]["observation_count"] == 14
    assert payload["current_set"]["observation_omitted_count"] == 2
    assert len(payload["current_set_observation_previews"]) == 12
    assert "note" not in payload["current_set_observation_previews"][0]


def test_interrupted_or_legacy_incomplete_research_jobs_resume() -> None:
    workspace = {
        "status": "finalized",
        "current_job": {"job_id": "running"},
        "memo_fields": {"title": "partial"},
        "processed_jobs": [
            {"job_id": "running"},
            {"job_id": "legacy"},
            {"job_id": "complete"},
        ],
        "investigation_jobs": [
            {"job_id": "running", "status": "in_progress", "questions": ["Q1"]},
            {"job_id": "legacy", "status": "mapped", "questions": ["Q2"]},
            {
                "job_id": "complete",
                "status": "mapped",
                "questions": ["Q3"],
                "question_outcomes": [
                    {"question": "Q3", "status": "answered", "note": "Answered.", "evidence_ids": ["ev-3"]}
                ],
            },
        ],
    }

    resumed, job_ids = prepare_workspace_jobs_for_resume(workspace)

    assert job_ids == ["running", "legacy"]
    assert [job["status"] for job in resumed["investigation_jobs"]] == ["planned", "planned", "mapped"]
    assert resumed["current_job"] is None
    assert resumed["memo_fields"] == {}
    assert resumed["processed_jobs"] == [{"job_id": "complete"}]


@pytest.mark.asyncio
async def test_research_job_requires_outcome_for_every_planned_question(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    evidence_id = str(uuid.uuid4())
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "current_job": {"job_id": "job_1"},
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "in_progress",
                "source_refs": ["S01"],
                "questions": ["How?", "Why?"],
                # Planner hints guide the agent; they must not become a semantic completion gate.
                "required_atom_kinds": ["mechanism"],
                "observations": [{"note": "Finding", "source_refs": ["S01"], "evidence_ids": [evidence_id]}],
                "atom_ids": [],
                "evidence_ids": [evidence_id],
            }
        ],
        "processed_jobs": [],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="job",
        current_job_id="job_1",
    )
    finish = next(tool for tool in tools if tool.name == "finish_research_job")

    rejected = await finish.handler(
        {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped.",
            "deferred_reason": "",
            "question_outcomes": [
                {"question": "How?", "status": "answered", "note": "Evidence answers it.", "evidence_ids": [evidence_id]}
            ],
        }
    )
    accepted = await finish.handler(
        {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped with one bounded gap.",
            "deferred_reason": "",
            "question_outcomes": [
                {"question": "How?", "status": "answered", "note": "Evidence answers it.", "evidence_ids": [evidence_id]},
                {"question": "Why?", "status": "deferred", "note": "No source answers it.", "evidence_ids": []},
            ],
        }
    )

    assert rejected["accepted"] is False
    assert rejected["missing_questions"] == ["Why?"]
    assert accepted["accepted"] is True
    assert workspace["investigation_jobs"][0]["question_outcomes"][1]["status"] == "deferred"


@pytest.mark.asyncio
async def test_mapped_triage_set_requires_each_source_mapped_or_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "source_order": ["S01", "S02"],
        "current_set": {"set_id": "set_1"},
        "source_sets": [
            {
                "set_id": "set_1",
                "status": "in_progress",
                "source_refs": ["S01", "S02"],
                "observations": [{"source_refs": ["S01"], "note": "Mapped S01."}],
                "written_actions": [],
                "evidence_ids": [],
            }
        ],
        "processed_sets": [],
        "mapped_sources": [],
        "deferred_sources": [],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_1",
    )
    finish = next(tool for tool in tools if tool.name == "finish_triage_source_set")

    rejected = await finish.handler(
        {"set_id": "set_1", "status": "mapped", "summary": "Partial.", "deferred_reason": "", "deferred_sources": []}
    )
    accepted = await finish.handler(
        {
            "set_id": "set_1",
            "status": "mapped",
            "summary": "Mapped S01; bounded S02.",
            "deferred_reason": "",
            "deferred_sources": [{"source_ref": "S02", "reason": "Unreadable source."}],
        }
    )

    assert rejected["unresolved_source_refs"] == ["S02"]
    assert accepted["accepted"] is True
    assert workspace["mapped_sources"] == ["S01"]
    assert workspace["deferred_sources"] == [{"source_ref": "S02", "reason": "Unreadable source."}]


@pytest.mark.asyncio
async def test_triage_plan_rejects_deferred_source_without_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "source_order": ["S01", "S02"],
        "source_sets": [{"set_id": "set_1", "source_refs": ["S01"]}],
        "deferred_sources": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="planner",
    )
    finalize = next(tool for tool in tools if tool.name == "finalize_triage_workspace_plan")

    result = await finalize.handler(
        {
            "deferred_sources": [{"source_refs": ["S02"], "reason": ""}],
            "summary": "Plan",
        }
    )

    assert result == {
        "accepted": False,
        "error": "Each deferred source requires an explicit reason",
        "source_refs": ["S02"],
    }
    assert workspace["deferred_sources"] == []


def test_model_context_uses_explicit_previews_and_reports_omitted_pages() -> None:
    long_claim = "x" * 900
    workspace = {
        "source_order": ["S01"],
        "atom_drafts": [
            {"atom_id": f"a{idx}", "kind": "mechanism", "title": f"Atom {idx}", "claim": long_claim}
            for idx in range(3)
        ],
        "investigation_jobs": [],
    }

    payload = research_workspace_summary_for_prompt(workspace, max_atoms=1)

    assert "claim" not in payload["atom_drafts"][0]
    assert payload["atom_drafts"][0]["claim_preview"] == long_claim[:700]
    assert payload["atom_drafts"][0]["claim_truncated"] is True
    assert payload["pages"]["atom_drafts"]["omitted_count"] == 2
    assert payload["pages"]["atom_drafts"]["next_cursor"] == "1"


@pytest.mark.asyncio
async def test_research_job_context_is_compact_and_pages_current_job_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    questions = [f"Question {idx}" for idx in range(8)]
    observations = [
        {
            "source_refs": ["S01"],
            "evidence_ids": [f"ev-{idx}"],
            "note": f"Observation {idx} " + ("x" * 800),
            "pending_questions": [],
        }
        for idx in range(3)
    ]
    workspace = {
        "source_order": ["S01"],
        "current_job": {"job_id": "job_1"},
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "label": "Mechanism",
                "story_job": "mechanism",
                "purpose": "Resolve the mechanism.",
                "source_refs": ["S01"],
                "questions": questions,
                "observations": observations,
                "status": "in_progress",
            }
        ],
    }
    payload = research_job_prompt_payload(workspace, "job_1", source_map_summary={})

    assert "note" not in payload["job"]["observations"][0]
    assert payload["job"]["observations"][0]["note_truncated"] is True
    assert "question" not in payload["job"]["questions"][0]
    assert payload["job"]["pages"]["questions"]["omitted_count"] == 2

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(source_cards=[_source(title="Source")]),
        mode="job",
        current_job_id="job_1",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")
    page = await page_tool.handler(
        {"section": "current_job_questions", "cursor": "6", "limit": 2}
    )

    assert [item["question_preview"] for item in page["items"]] == questions[6:]
    assert page["page"]["next_cursor"] == ""
    assert page["page"]["omitted_count"] == 0


@pytest.mark.asyncio
async def test_source_map_pages_are_retrievable_through_research_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    cards = [_source(title=f"Source {idx}").model_copy(update={"evidence_ids": [f"ev-{idx}"]}) for idx in range(3)]
    source_map = SourceMap(source_cards=cards)
    workspace = {"source_order": ["S01", "S02", "S03"], "investigation_jobs": []}

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=source_map,
        source_map=source_map,
        mode="planner",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")
    page = await page_tool.handler({"section": "source_map_cards", "cursor": "1", "limit": 1})

    assert page["accepted"] is True
    assert page["items"][0]["title"] == "Source 1"
    assert "evidence_ids" not in page["items"][0]
    assert page["page"]["next_cursor"] == "2"


def test_source_map_initial_context_is_compact_but_pages_restore_full_validated_items() -> None:
    source = _source(title="Primary")
    related = _source(title="Related")
    summary = "summary " * 180
    quote_text = "quote " * 220
    cluster_rationale = "cluster rationale " * 100
    relationship_rationale = "relationship rationale " * 90
    gap_text = "gap detail " * 160
    evidence_ids = [f"ev-{index}" for index in range(12)]
    source = source.model_copy(
        update={
            "summary": summary,
            "useful_for": ["mechanism", "context"],
            "gaps_or_cautions": ["A full caution that must survive compact context."],
            "evidence_ids": evidence_ids,
        }
    )
    source_map = SourceMap(
        source_cards=[source, related],
        quote_cards=[
            QuoteCard(
                source_id=source.source_id,
                evidence_id="ev-quote",
                quote=quote_text,
                locator="p. 4",
                narrative_use="Explain the mechanism.",
                confidence_reason="Canonical text match.",
            )
        ],
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="cluster-1",
                    label="Mechanism sources",
                    source_ids=[source.source_id, related.source_id],
                    evidence_ids=evidence_ids,
                    rationale=cluster_rationale,
                )
            ],
            source_relationships=[
                SourceRelationship(
                    source_id=source.source_id,
                    related_source_id=related.source_id,
                    relationship="background_for",
                    evidence_ids=evidence_ids,
                    rationale=relationship_rationale,
                )
            ],
            gaps=[gap_text],
        ),
    )

    initial = source_map_research_summary(source_map, source_map, include_evidence_ids=False)
    assert "summary" not in initial["source_cards"][0]
    assert initial["source_cards"][0]["summary_truncated"] is True
    assert "quote" not in initial["quote_cards"][0]
    assert initial["quote_cards"][0]["quote_truncated"] is True

    pages = {
        section: source_map_research_page(
            source_map,
            source_map,
            section=section,
            cursor=0,
            limit=40,
            include_evidence_ids=True,
        )
        for section in (
            "source_map_cards",
            "source_map_quotes",
            "source_map_clusters",
            "source_map_relationships",
            "source_map_gaps",
        )
    }

    assert pages["source_map_cards"]["items"][0]["summary"] == summary
    assert pages["source_map_quotes"]["items"][0]["quote"] == quote_text
    assert pages["source_map_clusters"]["items"][0]["rationale"] == cluster_rationale
    assert pages["source_map_relationships"]["items"][0]["rationale"] == relationship_rationale
    assert pages["source_map_gaps"]["items"][0]["gap"] == gap_text
    assert pages["source_map_cards"]["page"]["returned_count"] == 1
    assert pages["source_map_cards"]["page"]["next_cursor"] == "1"
    assert pages["source_map_cards"]["items"][0]["evidence_ids"] == evidence_ids[:8]
    assert pages["source_map_cards"]["items"][0]["evidence_id_omitted_count"] == 4
    for page in pages.values():
        model_facing_tool_output("list_research_workspace_items", page)

    hidden = source_map_research_page(
        source_map,
        source_map,
        section="source_map_cards",
        cursor=0,
        limit=1,
        include_evidence_ids=False,
    )
    assert "evidence_ids" not in hidden["items"][0]
    assert hidden["items"][0]["evidence_id_count"] == 12


@pytest.mark.asyncio
async def test_research_workspace_pages_return_full_validated_atoms_and_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    claim = "claim " * 250
    explanation = "explanation " * 220
    note_one = "observation one " * 150
    note_two = "observation two " * 150
    workspace = {
        "source_order": ["S01"],
        "atom_drafts": [
            {
                "atom_id": "atom-1",
                "job_id": "job-1",
                "kind": "mechanism",
                "title": "Mechanism",
                "claim": claim,
                "explanation": explanation,
                "story_value": "Listener value " * 60,
                "source_refs": ["S01"],
                "evidence_ids": ["backend-evidence-1"],
                "risk": "Specific bounded risk " * 50,
                "treatment_role_hint": "Use this mechanism after context.",
            }
        ],
        "investigation_jobs": [
            {
                "job_id": "job-1",
                "label": "Mechanism",
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": ["backend-evidence-1"],
                        "note": note_one,
                        "pending_questions": ["First full pending question."],
                    },
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": ["backend-evidence-2"],
                        "note": note_two,
                        "pending_questions": ["Second full pending question."],
                    },
                ],
            }
        ],
    }

    compact = research_workspace_summary_for_prompt(
        workspace,
        include_atom_evidence_ids=False,
        include_gap_evidence_ids=False,
    )
    assert "claim" not in compact["atom_drafts"][0]
    assert compact["atom_drafts"][0]["claim_truncated"] is True

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(source_cards=[_source(title="Source")]),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")
    atom_page = await page_tool.handler({"section": "atom_drafts", "cursor": "0", "limit": 40})
    observation_page = await page_tool.handler({"section": "observations", "cursor": "1", "limit": 40})

    assert atom_page["items"][0]["claim"] == claim
    assert atom_page["items"][0]["explanation"] == explanation
    assert atom_page["page"]["returned_count"] == 1
    assert observation_page["items"][0]["note"] == note_two
    assert observation_page["items"][0]["pending_questions"] == ["Second full pending question."]
    assert observation_page["page"]["next_cursor"] == ""
    assert "backend-evidence-1" not in json.dumps(atom_page)
    assert "backend-evidence-2" not in json.dumps(observation_page)
    assert len(json.dumps(atom_page)) < 12000
    assert len(json.dumps(observation_page)) < 12000
    assert model_facing_tool_output("list_research_workspace_items", atom_page) is atom_page
    assert model_facing_tool_output("list_research_workspace_items", observation_page) is observation_page


@pytest.mark.asyncio
async def test_research_workspace_pages_restore_full_jobs_gaps_risks_and_audit_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    purpose = "p" * 900
    job_summary = "s" * 1600
    deferred_reason = "d" * 1000
    question = "q" * 1000
    outcome_note = "o" * 1600
    gap_question = "g" * 1000
    resolution_note = "r" * 1800
    risk = "k" * 1800
    feedback_description = "f" * 1000
    suggested_repair = "x" * 1800
    hidden_evidence_id = "backend-evidence-id"
    workspace = {
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "label": "Mechanism",
                "story_job": "mechanism",
                "purpose": purpose,
                "source_refs": ["S01"],
                "required_atom_kinds": ["mechanism"],
                "priority": "high",
                "allow_broad_search": False,
                "status": "deferred",
                "attempt": 1,
                "summary": job_summary,
                "deferred_reason": deferred_reason,
                "questions": [question],
                "question_outcomes": [
                    {
                        "question": question,
                        "status": "deferred",
                        "note": outcome_note,
                        "evidence_ids": [hidden_evidence_id],
                    }
                ],
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [hidden_evidence_id],
            }
        ],
        "research_gaps": [
            {
                "gap_id": "gap_1",
                "question": gap_question,
                "category": "missing_source",
                "severity": "blocking",
                "treatment_relevance": "central",
                "status": "deferred",
                "source_refs": ["S01"],
                "atom_refs": ["A01"],
                "resolution_note": resolution_note,
                "evidence_ids": [hidden_evidence_id],
            }
        ],
        "risk_register": [
            {
                "risk": risk,
                "severity": "major",
                "source_refs": ["S01"],
                "atom_refs": ["A01"],
                "evidence_ids": [hidden_evidence_id],
            }
        ],
        "audit_repair_feedback": [
            {
                "target_step": "research_finalizer",
                "severity": "major",
                "description": feedback_description,
                "suggested_repair": suggested_repair,
                "atom_refs": ["A01"],
                "source_refs": ["S01"],
                "evidence_ids": [hidden_evidence_id],
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")
    pages = {
        "job": await page_tool.handler({"section": "jobs", "cursor": "0", "limit": 40}),
        "question": await page_tool.handler(
            {"section": "job_questions", "job_id": "job_1", "cursor": "0", "limit": 40}
        ),
        "outcome": await page_tool.handler(
            {
                "section": "job_question_outcomes",
                "job_id": "job_1",
                "cursor": "0",
                "limit": 40,
            }
        ),
        "gap": await page_tool.handler({"section": "research_gaps", "cursor": "0", "limit": 40}),
        "risk": await page_tool.handler({"section": "workspace_risks", "cursor": "0", "limit": 40}),
        "feedback": await page_tool.handler({"section": "audit_feedback", "cursor": "0", "limit": 40}),
    }

    assert pages["job"]["items"][0]["purpose"] == purpose
    assert pages["job"]["items"][0]["summary"] == job_summary
    assert pages["job"]["items"][0]["deferred_reason"] == deferred_reason
    assert pages["question"]["items"][0]["question"] == question
    assert pages["outcome"]["items"][0]["note"] == outcome_note
    assert pages["gap"]["items"][0]["question"] == gap_question
    assert pages["gap"]["items"][0]["resolution_note"] == resolution_note
    assert pages["risk"]["items"][0]["risk"] == risk
    assert pages["feedback"]["items"][0]["description"] == feedback_description
    assert pages["feedback"]["items"][0]["suggested_repair"] == suggested_repair
    for page in pages.values():
        assert hidden_evidence_id not in json.dumps(page)
        assert len(json.dumps(page)) < 12000
        assert model_facing_tool_output("list_research_workspace_items", page) is page

    compact = research_workspace_summary_for_prompt(
        workspace,
        include_atom_evidence_ids=False,
        include_gap_evidence_ids=False,
    )
    assert compact["jobs"][0]["summary_truncated"] is True
    assert compact["research_gaps"][0]["resolution_note_truncated"] is True
    assert compact["pages"]["workspace_risks"]["omitted_count"] == 1
    assert compact["audit_repair_feedback"][0]["suggested_repair_truncated"] is True


def test_triage_context_reports_source_set_page_and_preview_fields() -> None:
    workspace = {
        "source_sets": [
            {"set_id": f"set_{idx}", "status": "mapped", "findings_summary": "x" * 700}
            for idx in range(3)
        ]
    }

    payload = workspace_summary_for_prompt(workspace, max_sets=1)

    assert "findings_summary" not in payload["source_sets"][0]
    assert payload["source_sets"][0]["findings_summary_truncated"] is True
    assert payload["source_sets_page"]["omitted_count"] == 2

    last_page = workspace_summary_for_prompt(workspace, max_sets=1, set_cursor=2)
    assert last_page["source_sets_page"]["omitted_count"] == 0
    assert last_page["source_sets_page"]["next_cursor"] == ""


def test_triage_source_set_payload_is_compact_and_preserves_workspace() -> None:
    long_purpose = "p" * 600
    long_rationale = "r" * 1200
    long_note = "n" * 1600
    workspace = {
        "source_order": ["S01"],
        "source_sets": [
            {
                "set_id": "set_1",
                "label": "Primary set",
                "purpose": long_purpose,
                "rationale": long_rationale,
                "questions": ["What happened?"],
                "source_refs": ["S01"],
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": ["ev-1"],
                        "note": long_note,
                        "pending_questions": ["What remains?"],
                    }
                ],
                "written_actions": [
                    {
                        "tool_name": "upsert_source_card",
                        "source_refs": ["S01"],
                        "evidence_ids": ["ev-1"],
                    }
                ],
            }
        ],
        "source_capsules": {
            "S01": {
                "source_ref": "S01",
                "title": "Primary source",
                "summary_hint": "s" * 240,
                "outline": ["Section 1"],
                "windows": [
                    {
                        "locator": "Section 1",
                        "locator_type": "heading",
                        "preview_text": "w" * 400,
                    }
                ],
                "readability_warnings": ["No warning"],
                "readable": True,
            }
        },
    }

    payload = source_set_prompt_payload(workspace, "set_1")
    source_set = payload["set"]

    assert "purpose" not in source_set
    assert "rationale" not in source_set
    assert "questions" not in source_set
    assert "observations" not in source_set
    assert "written_actions" not in source_set
    assert source_set["purpose_truncated"] is True
    assert source_set["rationale_truncated"] is True
    assert source_set["observation_count"] == 1
    assert source_set["observation_previews"][0]["note_truncated"] is True
    assert payload["capsule_previews"][0]["summary_hint_truncated"] is True
    assert payload["capsule_previews"][0]["window_previews"][0]["text_truncated"] is True
    assert workspace["source_sets"][0]["purpose"] == long_purpose
    assert workspace["source_sets"][0]["observations"][0]["note"] == long_note


@pytest.mark.asyncio
async def test_triage_set_agent_can_restore_full_current_set_and_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    purpose = "p" * 600
    rationale = "r" * 1200
    findings = "f" * 1600
    note = "n" * 1600
    evidence_ids = [f"ev-{index}" for index in range(12)]
    workspace = {
        "source_order": ["S01"],
        "current_set": {"set_id": "set_1"},
        "source_sets": [
            {
                "set_id": "set_1",
                "label": "Primary set",
                "purpose": purpose,
                "source_refs": ["S01"],
                "rationale": rationale,
                "questions": ["q" * 280],
                "priority": "high",
                "status": "in_progress",
                "findings_summary": findings,
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": evidence_ids,
                        "note": note,
                        "pending_questions": ["pending " * 30],
                    }
                ],
                "written_actions": [],
            }
        ],
    }
    compact = source_set_prompt_payload(workspace, "set_1")
    assert "purpose" not in compact["set"]
    assert compact["set"]["purpose_truncated"] is True
    assert "note" not in compact["set"]["observation_previews"][0]

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    set_tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_1",
    )
    page_tool = next(tool for tool in set_tools if tool.name == "list_triage_source_sets")
    current_page = await page_tool.handler(
        {"section": "current_set", "set_id": "set_1", "cursor": "0", "limit": 1}
    )
    observation_page = await page_tool.handler(
        {"section": "observations", "set_id": "set_1", "cursor": "0", "limit": 1}
    )

    assert current_page["items"][0]["purpose"] == purpose
    assert current_page["items"][0]["rationale"] == rationale
    assert current_page["items"][0]["findings_summary"] == findings
    assert current_page["items"][0]["questions"] == ["q" * 280]
    assert observation_page["items"][0]["note"] == note
    assert observation_page["items"][0]["pending_questions"] == ["pending " * 30]
    assert observation_page["items"][0]["evidence_ids"] == evidence_ids[:8]
    assert observation_page["items"][0]["evidence_omitted_count"] == 4
    model_facing_tool_output("list_triage_source_sets", current_page)
    model_facing_tool_output("list_triage_source_sets", observation_page)

    finalizer_tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    finalizer_page_tool = next(tool for tool in finalizer_tools if tool.name == "list_triage_source_sets")
    hidden_page = await finalizer_page_tool.handler(
        {"section": "observations", "set_id": "set_1", "cursor": "0", "limit": 1}
    )
    assert "evidence_ids" not in hidden_page["items"][0]
    assert hidden_page["items"][0]["evidence_count"] == 12


@pytest.mark.asyncio
async def test_triage_pages_restore_full_plan_summary_and_deferred_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    plan_summary = "p" * 1600
    deferred_reason = "d" * 1000
    workspace = {
        "plan_summary": plan_summary,
        "deferred_sources": [
            {"source_ref": f"S{index:02d}", "reason": deferred_reason}
            for index in range(1, 26)
        ],
    }
    compact = workspace_summary_for_prompt(workspace)
    assert compact["plan_summary_truncated"] is True
    assert compact["deferred_source_count"] == 25
    assert compact["deferred_sources_page"]["omitted_count"] == 5

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_triage_source_sets")
    plan_page = await page_tool.handler(
        {"section": "plan_summary", "cursor": "0", "limit": 1}
    )
    deferred_page = await page_tool.handler(
        {"section": "deferred_sources", "cursor": "24", "limit": 1}
    )

    assert plan_page["items"][0]["summary"] == plan_summary
    assert deferred_page["items"][0] == {"source_ref": "S25", "reason": deferred_reason}
    assert deferred_page["page"]["next_cursor"] == ""
    assert model_facing_tool_output("list_triage_source_sets", plan_page) is plan_page
    assert model_facing_tool_output("list_triage_source_sets", deferred_page) is deferred_page


def test_exact_quote_match_is_strict_after_normalization() -> None:
    assert exact_quote_text_matches("A direct quote", "Before\nA  direct quote. After") is True
    assert exact_quote_text_matches("A different quote", "Before A direct quote. After") is False


@pytest.mark.asyncio
async def test_audit_can_page_evidence_omitted_from_initial_atom_preview() -> None:
    source = _source()
    evidence_ids = [str(uuid.uuid4()) for _ in range(6)]
    memo = ResearchMemo(
        title="Memo",
        thesis="Supported answer.",
        research_synthesis="Digest.",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                claim="Supported mechanism.",
                explanation="Supported mechanism.",
                source_ids=[source.source_id],
                evidence_ids=evidence_ids,
                story_value="Explains the system.",
            )
        ],
    )
    records = [
        {
            "id": evidence_id,
            "source_id": source.source_id,
            "locator_text": f"p. {idx}",
            "snippet_text": f"Evidence {idx}",
        }
        for idx, evidence_id in enumerate(evidence_ids, start=1)
    ]
    kwargs = {
        "research_memo": memo,
        "evidence_records": records,
        "context": build_source_ref_context(SourceMap(source_cards=[source])),
        "source_titles_by_id": {source.source_id: source.title},
    }
    initial, _, _ = audit_evidence_previews(**kwargs)
    catalog, evidence_id_by_ref, _ = audit_evidence_catalog(**kwargs)
    tools = audit_tools_with_evidence_refs(
        [],
        evidence_id_by_ref=evidence_id_by_ref,
        read_refs=set(),
        evidence_previews_by_atom_ref=catalog,
    )
    page_tool = next(tool for tool in tools if tool.name == "list_audit_evidence")
    page = await page_tool.handler({"atom_ref": "A01", "cursor": "4", "limit": 2})

    assert len(initial["A01"]) == 4
    assert [item["evidence_ref"] for item in page["items"]] == ["E005", "E006"]
    assert page["page"]["next_cursor"] == ""


@pytest.mark.asyncio
async def test_audit_context_pages_full_memo_and_workspace_fields_without_backend_ids() -> None:
    source = _source()
    topic = "topic " * 800
    long_claim = "claim " * 180
    long_summary = "summary " * 140
    memo = ResearchMemo(
        title="Memo",
        thesis="supported answer " * 60,
        research_synthesis="Internal digest.",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                claim=long_claim,
                explanation="explanation " * 120,
                source_ids=[source.source_id],
                evidence_ids=["backend-evidence-id"],
                story_value="Concrete listener value.",
            )
        ],
        source_coverage=[f"Coverage item {index}: " + "detail " * 80 for index in range(25)],
        open_questions=[f"Question {index}: what remains?" for index in range(25)],
        risk_register=[f"Risk {index}: preserve this bound." for index in range(25)],
    )
    workspace = {
        "processed_jobs": [
            {"job_id": f"job-{index}", "status": "finished", "summary": long_summary}
            for index in range(25)
        ],
        "research_gaps": [
            {
                "gap_id": f"gap-{index}",
                "question": "question " * 100,
                "resolution_note": "resolution " * 120,
                "status": "deferred",
                "evidence_ids": ["backend-evidence-id"],
            }
            for index in range(25)
        ],
        "risk_register": [
            {"risk": "workspace risk " * 100, "evidence_ids": ["backend-evidence-id"]}
            for _ in range(25)
        ],
    }
    run = SimpleNamespace(config={"user_prompt": topic, "duration_target": 1800, "language": "en"})
    inventory = SourceMap(source_cards=[source])
    payload = research_audit_input(
        run=run,
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=inventory,
        workspace=workspace,
        evidence_records=[],
    )

    assert len(payload["research_memo"]["source_coverage_previews"]) == 20
    assert payload["topic"] == topic
    assert payload["research_memo"]["pages"]["memo_source_coverage"]["omitted_count"] == 5
    assert len(payload["workspace"]["processed_jobs"]) == 20
    assert payload["workspace"]["processed_jobs"][0]["summary_truncated"] is True
    assert payload["workspace"]["pages"]["research_gaps"]["omitted_count"] == 5

    tool = audit_workspace_page_tool(
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=inventory,
        workspace=workspace,
        evidence_previews_by_atom_ref={"A01": []},
    )
    atom_page = await tool.handler({"section": "memo_atoms", "cursor": "0", "limit": 5})
    coverage_page = await tool.handler({"section": "memo_source_coverage", "cursor": "20", "limit": 5})
    gap_page = await tool.handler({"section": "research_gaps", "cursor": "0", "limit": 3})

    assert atom_page["items"][0]["claim"] == long_claim
    assert atom_page["page"]["returned_count"] == 1
    assert coverage_page["items"][-1]["text"] == memo.source_coverage[-1]
    assert coverage_page["page"]["next_cursor"] == ""
    assert gap_page["items"][0]["question"] == workspace["research_gaps"][0]["question"]
    assert "backend-evidence-id" not in str(atom_page)
    assert "backend-evidence-id" not in str(gap_page)


@pytest.mark.asyncio
async def test_exact_quote_verification_ignores_stale_corpus_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = uuid.uuid4()
    current_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    current = SimpleNamespace(
        id=current_id,
        source_id=source_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="current.txt",
    )
    stale = SimpleNamespace(
        id=stale_id,
        source_id=source_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="stale.txt",
    )

    class Result:
        def scalars(self):
            return [current, stale]

    class Db:
        async def execute(self, _query):
            return Result()

    toolset = SimpleNamespace(
        db=Db(),
        run_view=SimpleNamespace(selected_source_version_ids=[str(current_id)]),
    )
    monkeypatch.setattr(
        "app.generation_v2.tools.evidence_model_read_persistence.get_minio_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "app.generation_v2.tools.evidence_model_read_persistence.download_text",
        lambda _client, path: "The old exact quote." if path == "stale.txt" else "Current material only.",
    )

    verified = await EvidenceModelReadPersistenceMixin._quote_exists_in_canonical_text(
        toolset,  # type: ignore[arg-type]
        current,  # type: ignore[arg-type]
        "The old exact quote.",
    )

    assert verified is False


@pytest.mark.asyncio
async def test_audit_editorial_repair_persists_gap_and_enqueues_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {"research_gaps": [], "investigation_jobs": []}
    saved: list[dict] = []

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **kwargs):
        saved.append(dict(kwargs["workspace"]))
        return SimpleNamespace(payload=workspace)

    async def enqueue(_db, **_kwargs):
        return ["repair_job"]

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    monkeypatch.setattr(workspace_api, "enqueue_research_repair_jobs", enqueue)
    audit = ResearchAudit(
        passed=False,
        score=0.3,
        verdict="repair",
        issues=[
            ResearchAuditIssue(
                severity="blocking",
                target_step="editorial_research",
                description="Resolve the missing mechanism support.",
                suggested_repair="Read the existing source and save grounded evidence.",
            )
        ],
    )

    job_ids = await enqueue_audit_research_repair_jobs(None, run=run, audit=audit)  # type: ignore[arg-type]

    assert job_ids == ["repair_job"]
    assert saved[0]["research_gaps"][0]["question"] == "Resolve the missing mechanism support."
    assert saved[0]["memo_fields"] == {}
