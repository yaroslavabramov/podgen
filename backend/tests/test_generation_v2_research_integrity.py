from __future__ import annotations

import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.tool_sets import (
    TRIAGE_FINALIZER_TOOL_NAMES,
    TRIAGE_PLANNER_TOOL_NAMES,
    TRIAGE_SET_TOOL_NAMES,
)
from app.generation_v2.agents.audit_context import (
    audit_workspace_page_tool,
    normalize_research_audit,
    normalize_research_audit_references,
    research_audit_input,
)
from app.generation_v2.agents.audit_evidence_context import (
    audit_evidence_catalog,
    audit_evidence_previews,
    audit_tools_with_evidence_refs,
)
from app.generation_v2.pipeline.research_repair import (
    enqueue_audit_research_repair_jobs,
    enqueue_treatment_research_repair_jobs,
)
from app.generation_v2.pipeline.research_validation import validate_research_memo_evidence
from app.generation_v2.research_workspace import build_research_workspace_tools
from app.generation_v2.research_workspace_job_state import prepare_workspace_jobs_for_resume
from app.generation_v2.research_workspace_prompt_payloads import (
    research_finalizer_payload_for_prompt,
    research_job_prompt_payload,
    research_workspace_summary_for_prompt,
)
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
from app.generation_v2.source_triage_set_review import (
    record_semantic_review_page,
    semantic_review_page,
    set_semantic_review_signature,
)
from app.generation_v2.source_triage_set_state_views import question_workbench_signature
from app.generation_v2.research_source_map_prompting import (
    source_map_planner_summary,
    source_map_research_page,
    source_map_research_summary,
)
from app.generation_v2.runtime_checkpoint_context import (
    RESEARCH_JOB_RESUME_CONTRACT,
    build_checkpoint_resume_context,
)
from app.generation_v2.research_job_checkpoint_payloads import compact_research_job_checkpoint_payload
from app.generation_v2.runtime_checkpoint_restart import (
    checkpoint_restart_instructions,
    checkpoint_restart_message,
)
from app.generation_v2.runtime_checkpoint_paging import summarize_paginated_tool_traces
from app.generation_v2.runtime_contracts import default_checkpoint_policy
from app.generation_v2.runtime import AgentTool
from app.generation_v2.runtime_tool_output import model_facing_tool_output
from app.generation_v2.contracts import V2ToolName
from app.generation_v2.schemas import (
    EditorialAtom,
    EpisodeCritique,
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
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref
from app.generation_v2.source_triage_workspace import build_workspace_tools
from app.generation_v2.source_triage_workspace_prompting import (
    source_set_prompt_payload,
    workspace_summary_for_prompt,
)
from app.generation_v2.tools.evidence_core_utils import exact_quote_text_matches
from app.generation_v2.tools.evidence_model_read_persistence import EvidenceModelReadPersistenceMixin
from app.models.generation_v2 import SourceRepresentationKind, V2ToolTrace


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


def test_audit_references_are_normalized_to_current_public_inventory() -> None:
    source = _source()
    evidence_id = str(uuid.uuid4())
    audit = ResearchAudit(
        passed=False,
        score=0.4,
        verdict="repair",
        issues=[
            ResearchAuditIssue(
                severity="major",
                target_step="editorial_research",
                description="Repair grounding.",
                source_refs=["s01", source.source_id, "S99"],
                evidence_refs=["e001", "E999"],
                evidence_ids=[evidence_id, "invented-evidence-id"],
            )
        ],
    )

    normalized = normalize_research_audit_references(
        audit,
        context=build_source_ref_context(SourceMap(source_cards=[source])),
        evidence_id_by_ref={"E001": evidence_id},
    )
    issue = normalized.issues[0]

    assert issue.source_refs == ["S01"]
    assert issue.evidence_refs == ["E001"]
    assert issue.evidence_ids == [evidence_id]
    assert "unknown source reference" in issue.suggested_repair
    assert "unknown evidence reference" in issue.suggested_repair
    assert "S99" not in issue.suggested_repair
    assert "E999" not in issue.suggested_repair


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
async def test_audit_required_evidence_reads_are_enforced_for_any_episode_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.agents import audit_agent

    class Runtime:
        async def call_structured(self, **_kwargs):
            return ResearchAudit(passed=True, score=1.0, verdict="Passed without reading.")

    monkeypatch.setattr(
        audit_agent,
        "_audit_evidence_catalog",
        lambda **_kwargs: ({"A01": []}, {"E001": "backend-evidence-id"}, {"E001"}),
    )
    audit = await audit_agent.audit_research_memo(
        None,  # type: ignore[arg-type]
        SimpleNamespace(
            id=uuid.uuid4(),
            config={"user_prompt": "Topic", "duration_target": 900, "language": "en"},
        ),
        Runtime(),  # type: ignore[arg-type]
        research_memo=ResearchMemo(
            title="Memo",
            thesis="Supported answer.",
            research_synthesis="Internal digest.",
            editorial_atoms=[],
        ),
    )

    assert audit.passed is False
    assert audit.blocking is True
    assert audit.repair_targets == ["research_audit"]
    assert audit.issues[0].evidence_refs == ["E001"]


@pytest.mark.asyncio
async def test_runtime_fallback_cannot_mark_an_unaudited_memo_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.generation_v2.agents import audit_agent
    from app.generation_v2.runtime import V2RuntimeUnavailable

    class Runtime:
        fallback_marked = False

        async def call_structured(self, **_kwargs):
            raise V2RuntimeUnavailable("offline")

        def mark_fallback_used(self, _phase: str, _agent_name: str) -> None:
            self.fallback_marked = True

    monkeypatch.setattr(settings, "generation_v2_allow_runtime_fallback", True)
    runtime = Runtime()
    audit = await audit_agent.audit_research_memo(
        None,  # type: ignore[arg-type]
        SimpleNamespace(id=uuid.uuid4(), config={"duration_target": 1800}),
        runtime,  # type: ignore[arg-type]
        research_memo=ResearchMemo(
            title="Memo",
            thesis="Supported answer.",
            research_synthesis="Internal digest.",
            editorial_atoms=[],
        ),
    )

    assert runtime.fallback_marked is True
    assert audit.passed is False
    assert audit.blocking is True
    assert audit.repair_targets == ["research_audit"]


@pytest.mark.asyncio
async def test_repair_budget_survives_runner_recreation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.pipeline.editorial_research_stage import EditorialResearchStageRunner
    from app.generation_v2 import research_workspace_persistence

    run_id = uuid.uuid4()
    stored_workspace: dict = {"repair_budget": {}}

    async def load_workspace(_db, _run_id):
        return {**stored_workspace, "repair_budget": dict(stored_workspace.get("repair_budget") or {})}

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
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

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
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

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
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
    assert operation["payload"]["repair_job_ids"] == [stored_workspace["investigation_jobs"][0]["job_id"]]
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

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
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

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
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

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
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
    assert (
        reserve_repair_operation_in_workspace(
            stored_workspace,
            TARGETED_REPAIR_BUDGET,
            operation_kind="research_finalizer",
        )
        is None
    )


@pytest.mark.asyncio
async def test_in_progress_triage_repair_survives_new_corpus_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_module

    source = _source()
    inventory = SourceMap(source_cards=[source])
    old_evidence_id = str(uuid.uuid4())
    new_evidence_id = str(uuid.uuid4())
    existing = {
        "schema_version": "source_triage_workspace.v3",
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
                        "evidence_ids": [old_evidence_id],
                        "written_actions": [{"evidence_ids": [old_evidence_id]}],
                        "observations": [{"evidence_ids": [old_evidence_id]}],
                    }
                ],
            },
        },
    }
    saved: dict = {}

    async def load_workspace(_db, _run_id):
        return existing

    async def save_workspace(_db, *, generation_run_id, workspace, status=None, **_kwargs):
        del generation_run_id, status
        saved.update(workspace)
        return SimpleNamespace(payload=workspace)

    async def carry_evidence(*_args, **_kwargs):
        return {old_evidence_id: new_evidence_id}, set()

    carried_actions: list[dict] = []

    async def carry_actions(*_args, **kwargs):
        carried_actions.append(kwargs)
        return 0

    monkeypatch.setattr(workspace_module, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(workspace_module, "_carry_evidence_to_current_attempt", carry_evidence)
    monkeypatch.setattr(
        workspace_module,
        "_carry_source_map_write_actions_to_current_attempt",
        carry_actions,
    )

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
    assert workspace["source_sets"][0]["evidence_ids"] == [new_evidence_id]
    assert workspace["source_sets"][0]["written_actions"] == [{"evidence_ids": [new_evidence_id]}]
    assert workspace["source_sets"][0]["observations"] == [{"evidence_ids": [new_evidence_id]}]
    assert carried_actions[0]["source_attempt"] == 1
    assert carried_actions[0]["evidence_id_map"] == {old_evidence_id: new_evidence_id}
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


def test_checkpoint_restart_instructions_match_the_active_role() -> None:
    planner = checkpoint_restart_instructions(
        {
            "phase": "v2_source_triage",
            "agent_name": "source_triage_planner_agent",
            "corpus": {"source_inventory_page": {"complete": True}},
        }
    )
    triage_set = checkpoint_restart_instructions(
        {"phase": "v2_source_triage", "agent_name": "source_triage_set_agent_set_1"}
    )
    finalizer = checkpoint_restart_instructions(
        {"phase": "v2_source_triage", "agent_name": "source_triage_finalizer_agent"}
    )
    research = checkpoint_restart_instructions(
        {"phase": "v2_editorial_research", "agent_name": "research_job_agent_job_1"}
    )

    assert "complete catalog" in planner
    assert "do not restart full-corpus capsule paging" in planner
    assert "upsert_source_card" not in planner
    assert "skim_source" not in planner
    assert "finalize_source_map" not in planner
    assert "finish_triage_source_set" in triage_set
    assert "save_source_analysis" in triage_set
    assert "read_triage_set_state" in triage_set
    assert "upsert_source_card" not in triage_set
    assert "source reads only for unfinished work" in triage_set
    assert "list_source_capsules" not in triage_set
    assert "finalize_source_map" not in triage_set
    assert "finalize_source_map" in finalizer
    assert "upsert_source_card" not in finalizer
    assert "skim_source" not in finalizer
    assert "read_source_window" not in finalizer
    assert "compare_source_evidence" not in finalizer
    assert "record_source_relationship" not in finalizer
    assert "finish_research_job" in research
    assert "upsert_source_card" not in research
    assert "list_sources" not in planner
    assert "list_sources" not in triage_set
    assert "list_sources" not in finalizer

    known_tool_names = {item.value for item in V2ToolName}

    def mentioned(text: str) -> set[str]:
        return {name for name in known_tool_names if name in text}

    assert mentioned(planner) <= TRIAGE_PLANNER_TOOL_NAMES
    assert mentioned(triage_set) <= TRIAGE_SET_TOOL_NAMES
    assert mentioned(finalizer) <= TRIAGE_FINALIZER_TOOL_NAMES


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
        "source_capsules": {"S42": {"source_ref": "S42", "title": "Current source", "readable": True}},
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
            {
                "job_id": "stale-requirement",
                "status": "mapped",
                "questions": ["Q4"],
                "coverage_requirements": [{"requirement_id": "r4", "description": "Requirement."}],
                "question_outcomes": [
                    {"question": "Q4", "status": "answered", "note": "Answered.", "evidence_ids": ["ev-4"]}
                ],
                "requirement_outcomes": [
                    {"requirement_id": "r4", "status": "deferred", "note": "Still open.", "evidence_ids": []}
                ],
            },
        ],
    }

    resumed, job_ids = prepare_workspace_jobs_for_resume(workspace)

    assert job_ids == ["running", "legacy", "stale-requirement"]
    assert [job["status"] for job in resumed["investigation_jobs"]] == [
        "planned",
        "planned",
        "mapped",
        "planned",
    ]
    assert resumed["current_job"] is None
    assert resumed["memo_fields"] == {}
    assert resumed["processed_jobs"] == [{"job_id": "complete"}]


def test_leaf_research_eval_report_accepts_deferred_requirement_only_with_gap() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "job-1",
        "status": "deferred",
        "questions": ["Q"],
        "coverage_requirements": [{"requirement_id": "req-hk10"}],
        "question_outcomes": [
            {"question": "Q", "status": "deferred", "note": "One requirement remains open.", "evidence_ids": []}
        ],
        "requirement_outcomes": [
            {
                "requirement_id": "req-hk10",
                "status": "deferred",
                "note": "HK$10 exception still unresolved.",
                "evidence_ids": [],
            }
        ],
        "atom_ids": [],
        "observations": [{"note": "Observation.", "source_refs": ["S03"], "evidence_ids": ["ev-1"]}],
    }

    missing_gap_report = _leaf_job_structural_report({"atom_drafts": []}, job)
    with_gap_report = _leaf_job_structural_report(
        {
            "atom_drafts": [],
            "research_gaps": [
                {
                    "job_id": "job-1",
                    "requirement_id": "req-hk10",
                    "category": "open_requirement",
                    "status": "deferred",
                    "question": "Why HK$10 stays exceptional.",
                }
            ],
        },
        job,
    )

    assert missing_gap_report["passed"] is False
    assert missing_gap_report["deferred_requirement_ids_without_gap"] == ["req-hk10"]
    assert with_gap_report["passed"] is True
    assert with_gap_report["deferred_requirement_ids"] == ["req-hk10"]
    assert with_gap_report["requirement_outcomes"][0]["note"] == "HK$10 exception still unresolved."
    assert with_gap_report["observation_previews"][0]["source_refs"] == ["S03"]


@pytest.mark.asyncio
async def test_research_leaf_eval_guard_uses_delta_budget_and_allows_missing_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_v2.research_leaf_eval_runner as runner
    import app.generation_v2.research_leaf_eval_contract as contract
    from app.generation_v2.triage_eval_support import EvalBudgetExceeded

    async def within_budget(_db, _run_id):
        return {"totals": {"estimated_token_cost_usd": 1.05, "max_call_cost_usd": 0.10}}

    monkeypatch.setattr(contract, "summarize_tool_trace_costs", within_budget)
    guard = runner.ResearchLeafEvalCostGuard(max_delta_cost_usd=0.25, baseline_cost_usd=0.95)

    await guard(
        db=None,
        generation_run_id=uuid.uuid4(),
        requested_model="gpt-5.4-mini",
        max_output_tokens=None,
        server_compaction_threshold=None,
    )

    async def over_budget(_db, _run_id):
        return {"totals": {"estimated_token_cost_usd": 1.30, "max_call_cost_usd": 0.15}}

    monkeypatch.setattr(contract, "summarize_tool_trace_costs", over_budget)
    with pytest.raises(EvalBudgetExceeded):
        await guard(
            db=None,
            generation_run_id=uuid.uuid4(),
            requested_model="gpt-5.4-mini",
            max_output_tokens=None,
            server_compaction_threshold=None,
        )


def test_leaf_research_eval_rejects_zero_progress_all_deferred_hk_canary() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "Deferred.", "evidence_ids": []}],
        "requirement_outcomes": [
            {"requirement_id": "q_hkd_mech_r1", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
            {"requirement_id": "q_hkd_mech_r2", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
            {"requirement_id": "q_hkd_mech_r3", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
            {"requirement_id": "q_hkd_mech_r4", "status": "deferred", "note": "HK$10 remains unresolved.", "evidence_ids": []},
        ],
        "evidence_ids": [],
        "observations": [],
        "atom_ids": [],
    }
    workspace = {
        "atom_drafts": [],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": requirement_id,
                "category": "open_requirement",
                "status": "deferred",
                "question": f"{requirement_id} open",
                "resolution_note": "HK$10 unresolved." if requirement_id == "q_hkd_mech_r4" else "Deferred.",
            }
            for requirement_id in ("q_hkd_mech_r1", "q_hkd_mech_r2", "q_hkd_mech_r3", "q_hkd_mech_r4")
        ],
    }

    report = _leaf_job_structural_report(workspace, job)

    assert report["passed"] is False
    assert "leaf canary made no persisted research progress" in report["issues"]
    assert "semantic canary: q_hkd_mech_r1 must be answered" in report["issues"]


def _hk_current_mechanics_canary_fixture(*, emitters_text=None, r4_gap_note=None):
    emitters_text = emitters_text or (
        "HKMA выпускает HK$10, а HSBC, Standard Chartered и Bank of China выпускают HK$20, HK$50, "
        "HK$100, HK$500 и HK$1000."
    )
    r4_gap_note = r4_gap_note or "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом."
    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "HK$10 residual remains open.", "evidence_ids": []}],
        "requirement_outcomes": [
            {
                "requirement_id": "q_hkd_mech_r1",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_emitters"],
                "supporting_atom_ids": ["atom_emitters"],
            },
            {
                "requirement_id": "q_hkd_mech_r2",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_ci"],
                "supporting_atom_ids": ["atom_ci"],
            },
            {
                "requirement_id": "q_hkd_mech_r3",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_lers"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {
                "requirement_id": "q_hkd_mech_r4",
                "status": "deferred",
                "note": r4_gap_note,
                "evidence_ids": [],
                "supporting_atom_ids": [],
            },
        ],
        "evidence_ids": ["ev_emitters", "ev_ci", "ev_lers"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev_emitters"]}],
        "atom_ids": ["atom_emitters", "atom_ci", "atom_lers"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Текущие эмитенты",
                "claim": emitters_text,
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_emitters"],
            },
            {
                "atom_id": "atom_ci",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Обеспечение через сертификаты",
                "claim": (
                    "HKMA использует Exchange Fund и Certificates of Indebtedness как механизм "
                    "обеспечения выпуска банкнот."
                ),
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_ci"],
            },
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Ограничение LERS",
                "claim": "Выпуск банкнот работает внутри LERS/currency board с курсом 7.80.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_lers"],
            },
        ],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": "q_hkd_mech_r4",
                "category": "open_requirement",
                "status": "deferred",
                "question": "Why HK$10 remains government-issued.",
                "resolution_note": r4_gap_note,
            }
        ],
    }
    evidence_text_by_id = {
        "ev_emitters": emitters_text,
        "ev_ci": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
        "ev_lers": "Banknote issuance operates under the LERS currency board at 7.80.",
    }
    return workspace, job, evidence_text_by_id


def test_leaf_research_eval_rejects_missing_exact_denominations() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    workspace, job, evidence_text_by_id = _hk_current_mechanics_canary_fixture(
        emitters_text=(
            "HSBC, Standard Chartered and Bank of China issue HK$20, HK$500 and HK$1000 denominations."
        )
    )

    report = _leaf_job_structural_report(workspace, job, evidence_text_by_id=evidence_text_by_id)

    assert report["passed"] is False
    assert any("q_hkd_mech_r1 evidence-backed material is missing expected domain terms" in issue for issue in report["issues"])


def test_leaf_research_eval_rejects_missing_exact_russian_denominations() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    workspace, job, evidence_text_by_id = _hk_current_mechanics_canary_fixture(
        emitters_text=(
            "HSBC, Standard Chartered and Bank of China issue номинал 20, номинал 500 and номинал 1000."
        )
    )

    report = _leaf_job_structural_report(workspace, job, evidence_text_by_id=evidence_text_by_id)

    assert report["passed"] is False
    assert any("q_hkd_mech_r1 evidence-backed material is missing expected domain terms" in issue for issue in report["issues"])


def test_leaf_research_eval_rejects_hk10_gap_without_private_bank_contrast() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    workspace, job, evidence_text_by_id = _hk_current_mechanics_canary_fixture(
        r4_gap_note=(
            "HK$10: not explained why HKMA keeps a government banknote / "
            "не объяснено, почему HKMA сохраняет государственную банкноту."
        )
    )

    report = _leaf_job_structural_report(workspace, job, evidence_text_by_id=evidence_text_by_id)

    assert report["passed"] is False
    assert any(
        "q_hkd_mech_r4 deferred outcome/gap text is missing expected residual HK$10 terms" in issue
        for issue in report["issues"]
    )


def test_leaf_research_eval_accepts_hk10_gap_with_private_bank_genitive() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    workspace, job, evidence_text_by_id = _hk_current_mechanics_canary_fixture(
        r4_gap_note="HK$10: не объяснено, почему HKMA/правительство сохраняет выпуск вместо частных банков."
    )

    report = _leaf_job_structural_report(workspace, job, evidence_text_by_id=evidence_text_by_id)

    assert report["passed"] is True


def test_leaf_research_eval_rejects_english_authored_atom_prose() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    workspace, job, evidence_text_by_id = _hk_current_mechanics_canary_fixture()
    workspace["atom_drafts"][0]["title"] = "Current issuers"
    workspace["atom_drafts"][0]["claim"] = (
        "HKMA issues HK$10, while HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, "
        "HK$100, HK$500 and HK$1000 denominations."
    )

    report = _leaf_job_structural_report(workspace, job, evidence_text_by_id=evidence_text_by_id)

    assert report["passed"] is False
    assert any("job atom prose should be written in requested Russian language" in issue for issue in report["issues"])


def test_leaf_research_eval_requires_explicit_supporting_atom_ids_for_answered_requirement() -> None:
    from app.generation_v2.research_leaf_eval_runner import _answered_requirement_atom_check

    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Текущие эмитенты",
                "claim": (
                    "HKMA выпускает HK$10, а HSBC, Standard Chartered и Bank of China выпускают HK$20, HK$50, "
                    "HK$100, HK$500 и HK$1000."
                ),
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_emitters"],
            }
        ]
    }
    job = {"job_id": "brief_02", "atom_ids": ["atom_emitters"]}
    term_groups = [
        ["HSBC"],
        ["Standard Chartered"],
        ["Bank of China"],
        ["HK$20"],
        ["HK$50"],
        ["HK$100"],
        ["HK$500"],
        ["HK$1000"],
    ]
    evidence_text_by_id = {
        "ev_emitters": (
            "HKMA issues HK$10, while HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, "
            "HK$500 and HK$1000."
        )
    }

    missing_support = _answered_requirement_atom_check(
        workspace,
        job,
        requirement_id="q_hkd_mech_r1",
        evidence_ids=["ev_emitters"],
        supporting_atom_ids=[],
        evidence_text_by_id=evidence_text_by_id,
        term_groups=term_groups,
    )
    explicit_support = _answered_requirement_atom_check(
        workspace,
        job,
        requirement_id="q_hkd_mech_r1",
        evidence_ids=["ev_emitters"],
        supporting_atom_ids=["atom_emitters"],
        evidence_text_by_id=evidence_text_by_id,
        term_groups=term_groups,
    )

    assert missing_support["passed"] is False
    assert missing_support["error"] == "answered requirement must list supporting_atom_ids explicitly"
    assert explicit_support["passed"] is True


def test_research_leaf_eval_runtime_overrides_only_research_job_model() -> None:
    from app.generation_v2.contracts import V2AgentName, V2AgentPrefix, V2Phase
    from app.generation_v2.research_leaf_eval_runner import (
        ResearchLeafEvalRuntime,
        _validate_eval_model_and_effort,
    )

    model, effort = _validate_eval_model_and_effort("gpt-5.4-2026-03-05", "medium")
    runtime = ResearchLeafEvalRuntime(
        model_override=model,
        reasoning_effort=effort,
        before_provider_call=None,
    )

    assert runtime.reasoning_effort == "medium"
    assert _validate_eval_model_and_effort("gpt-5.4-mini-2026-03-17", "none")[1] == "none"
    with pytest.raises(ValueError):
        _validate_eval_model_and_effort("gpt-5.4-mini-2026-03-17", "max")
    assert runtime.model_for(V2Phase.EDITORIAL_RESEARCH, V2AgentPrefix.RESEARCH_JOB.value + "brief_02") == (
        "gpt-5.4-2026-03-05"
    )
    assert runtime.model_for(V2Phase.EDITORIAL_RESEARCH, V2AgentName.RESEARCH_PLANNER) != "gpt-5.4-2026-03-05"


def test_research_leaf_eval_clone_payload_rewrites_lineage_ids() -> None:
    from app.generation_v2.research_leaf_eval_runner import _rewrite_clone_payload

    source_run_id = uuid.uuid4()
    eval_run_id = uuid.uuid4()
    source_view_id = uuid.uuid4()
    eval_view_id = uuid.uuid4()
    old_evidence_id = str(uuid.uuid4())
    new_evidence_id = str(uuid.uuid4())
    old_artifact_id = str(uuid.uuid4())
    new_artifact_id = str(uuid.uuid4())

    payload = {
        "generation_run_id": str(source_run_id),
        "run_view_id": str(source_view_id),
        "evidence_ids": [old_evidence_id],
        "nested": {"artifact_id": old_artifact_id},
    }

    rewritten = _rewrite_clone_payload(
        payload,
        evidence_id_map={old_evidence_id: new_evidence_id},
        artifact_id_map={old_artifact_id: new_artifact_id},
        source_run_id=source_run_id,
        eval_run_id=eval_run_id,
        source_view_id=source_view_id,
        eval_view_id=eval_view_id,
    )

    assert rewritten["generation_run_id"] == str(eval_run_id)
    assert rewritten["run_view_id"] == str(eval_view_id)
    assert rewritten["evidence_ids"] == [new_evidence_id]
    assert rewritten["nested"]["artifact_id"] == new_artifact_id


def test_leaf_research_eval_accepts_hk_canary_with_answered_core_and_residual_gap() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "HK$10 residual remains open.", "evidence_ids": []}],
        "requirement_outcomes": [
            {
                "requirement_id": "q_hkd_mech_r1",
                "status": "answered",
                "note": "HSBC, Standard Chartered and Bank of China issue private banknotes.",
                "evidence_ids": ["ev1"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {
                "requirement_id": "q_hkd_mech_r2",
                "status": "answered",
                "note": "HKMA acts through the Exchange Fund.",
                "evidence_ids": ["ev2"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {
                "requirement_id": "q_hkd_mech_r3",
                "status": "answered",
                "note": "The LERS currency board keeps the rate at 7.80.",
                "evidence_ids": ["ev3"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {
                "requirement_id": "q_hkd_mech_r4",
                "status": "deferred",
                "note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
                "evidence_ids": [],
                "supporting_atom_ids": [],
            },
        ],
        "evidence_ids": ["ev1", "ev2", "ev3"],
        "source_findings": [
            {
                "finding_id": "f2",
                "statement": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
                "support_explanation": "",
                "evidence_ids": ["ev2"],
            },
        ],
        "observations": [
            {
                "note": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 banknotes.",
                "source_refs": ["S03"],
                "evidence_ids": ["ev1"],
            }
        ],
        "atom_ids": ["atom_lers"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Механизм текущей эмиссии банкнот Гонконга",
                "claim": (
                    "HSBC, Standard Chartered и Bank of China выпускают HK$20, HK$50, HK$100, HK$500 "
                    "и HK$1,000 через механизм HKMA / Exchange Fund / Certificates of Indebtedness "
                    "внутри LERS/currency board на уровне 7.80."
                ),
                "explanation": (
                    "Атом связывает частных эмитентов, сертификаты задолженности, Exchange Fund и LERS "
                    "в один проверенный механизм."
                ),
                "story_value": "",
                "evidence_ids": ["ev1", "ev2", "ev3"],
            }
        ],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": "q_hkd_mech_r4",
                "category": "open_requirement",
                "status": "deferred",
                "question": "Why HK$10 remains government-issued.",
                "resolution_note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
            }
        ],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev1": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
            "ev2": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev3": "Banknote issuance operates under the LERS currency board at 7.80.",
        },
    )

    assert report["passed"] is True
    assert report["semantic_canary"]["applied"] is True


def test_leaf_research_eval_accepts_separate_atoms_for_hk_core_requirements() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "HK$10 residual remains open.", "evidence_ids": []}],
        "requirement_outcomes": [
            {
                "requirement_id": "q_hkd_mech_r1",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev1"],
                "supporting_atom_ids": ["atom_emitters"],
            },
            {
                "requirement_id": "q_hkd_mech_r2",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev2"],
                "supporting_atom_ids": ["atom_ci"],
            },
            {
                "requirement_id": "q_hkd_mech_r3",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev3"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {
                "requirement_id": "q_hkd_mech_r4",
                "status": "deferred",
                "note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
                "evidence_ids": [],
                "supporting_atom_ids": [],
            },
        ],
        "evidence_ids": ["ev1", "ev2", "ev3"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev1"]}],
        "atom_ids": ["atom_emitters", "atom_ci", "atom_lers"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Текущие эмитенты",
                "claim": (
                    "HSBC, Standard Chartered и Bank of China выпускают HK$20, HK$50, HK$100, HK$500 "
                    "и HK$1,000."
                ),
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev1"],
            },
            {
                "atom_id": "atom_ci",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Обеспечение сертификатами",
                "claim": (
                    "HKMA использует Exchange Fund и Certificates of Indebtedness для обеспечения "
                    "выпуска банкнот."
                ),
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev2"],
            },
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Ограничение LERS",
                "claim": "Выпуск банкнот работает внутри LERS/currency board с курсом 7.80.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev3"],
            },
        ],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": "q_hkd_mech_r4",
                "category": "open_requirement",
                "status": "deferred",
                "question": "Why HK$10 remains government-issued.",
                "resolution_note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
            }
        ],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev1": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
            "ev2": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev3": "Banknote issuance operates under the LERS currency board at 7.80.",
        },
    )

    assert report["passed"] is True


def test_leaf_research_eval_rejects_answered_hk10_without_authored_atom() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "mapped",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "answered", "note": "Answered.", "evidence_ids": ["ev1", "ev2", "ev3", "ev4"]}],
        "requirement_outcomes": [
            {"requirement_id": "q_hkd_mech_r1", "status": "answered", "note": "Answered.", "evidence_ids": ["ev1"]},
            {"requirement_id": "q_hkd_mech_r2", "status": "answered", "note": "Answered.", "evidence_ids": ["ev2"]},
            {"requirement_id": "q_hkd_mech_r3", "status": "answered", "note": "Answered.", "evidence_ids": ["ev3"]},
            {"requirement_id": "q_hkd_mech_r4", "status": "answered", "note": "Answered.", "evidence_ids": ["ev4"]},
        ],
        "evidence_ids": ["ev1", "ev2", "ev3", "ev4"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev1"]}],
        "atom_ids": ["atom_emitters", "atom_ci", "atom_lers"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Current issuers",
                "claim": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev1"],
            },
            {
                "atom_id": "atom_ci",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Certificate backing",
                "claim": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back banknote issuance.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev2"],
            },
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "LERS constraint",
                "claim": "Banknote issuance operates under the LERS currency board at 7.80.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev3"],
            },
        ],
        "research_gaps": [],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev1": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
            "ev2": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev3": "Banknote issuance operates under the LERS currency board at 7.80.",
            "ev4": "HK$10 was kept because the public demanded a 10-dollar banknote.",
        },
    )

    assert report["passed"] is False
    assert any(
        "q_hkd_mech_r4 answered outcome needs an authored atom using the same evidence" in issue
        for issue in report["issues"]
    )


def test_leaf_research_eval_rejects_answered_hk10_public_demand_as_residual_answer() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "mapped",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "answered", "note": "Answered.", "evidence_ids": ["ev_emitters", "ev_ci", "ev_lers", "ev_hk10"]}],
        "requirement_outcomes": [
            {
                "requirement_id": "q_hkd_mech_r1",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_emitters"],
                "supporting_atom_ids": ["atom_emitters"],
            },
            {
                "requirement_id": "q_hkd_mech_r2",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_ci"],
                "supporting_atom_ids": ["atom_ci"],
            },
            {
                "requirement_id": "q_hkd_mech_r3",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_lers"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {"requirement_id": "q_hkd_mech_r4", "status": "answered", "note": "Answered.", "evidence_ids": ["ev_hk10"]},
        ],
        "evidence_ids": ["ev_emitters", "ev_ci", "ev_lers", "ev_hk10"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev_emitters"]}],
        "atom_ids": ["atom_emitters", "atom_ci", "atom_lers", "atom_hk10"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Current issuers",
                "claim": "HKMA issues HK$10; The Hong Kong Shanghai Banking Corporation, Standard Chartered Hong Kong and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_emitters"],
            },
            {
                "atom_id": "atom_ci",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Certificate backing",
                "claim": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back banknote issuance.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_ci"],
            },
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "LERS constraint",
                "claim": "Banknote issuance operates under the LERS currency board at 7.80.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_lers"],
            },
            {
                "atom_id": "atom_hk10",
                "job_id": "brief_02",
                "kind": "exception",
                "title": "HK$10 exception",
                "claim": "HK$10 remained a government-issued banknote because HKMA issued its own HK$10 notes in response to public demand after the coin-replacement attempt.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_hk10"],
            },
        ],
        "research_gaps": [],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev_emitters": "The HKMA issues the 10-dollar note and the other three banks issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations: The Hong Kong Shanghai Banking Corporation, Standard Chartered Hong Kong and Bank of China.",
            "ev_ci": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev_lers": "Banknote issuance operates under the LERS currency board at 7.80.",
            "ev_hk10": "Between 1994 and 2002 an attempt was made to replace privately issued HK$10 notes with coins. In response to public demand for the continuation of this note, the HKMA issued its own HK$10 notes.",
        },
    )

    assert report["passed"] is False
    assert any(
        "q_hkd_mech_r4 evidence-backed material is missing expected residual HK$10 terms" in issue
        for issue in report["issues"]
    )


def test_leaf_research_eval_accepts_hk10_public_demand_atom_with_residual_gap() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "HK$10 residual remains open.", "evidence_ids": []}],
        "requirement_outcomes": [
            {
                "requirement_id": "q_hkd_mech_r1",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_emitters"],
                "supporting_atom_ids": ["atom_emitters"],
            },
            {
                "requirement_id": "q_hkd_mech_r2",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_ci"],
                "supporting_atom_ids": ["atom_ci"],
            },
            {
                "requirement_id": "q_hkd_mech_r3",
                "status": "answered",
                "note": "Answered.",
                "evidence_ids": ["ev_lers"],
                "supporting_atom_ids": ["atom_lers"],
            },
            {
                "requirement_id": "q_hkd_mech_r4",
                "status": "deferred",
                "note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
                "evidence_ids": [],
                "supporting_atom_ids": [],
            },
        ],
        "evidence_ids": ["ev_emitters", "ev_ci", "ev_lers", "ev_hk10"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev_emitters"]}],
        "atom_ids": ["atom_emitters", "atom_ci", "atom_lers", "atom_hk10"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Текущие эмитенты",
                "claim": (
                    "HKMA выпускает HK$10, а The Hong Kong Shanghai Banking Corporation, "
                    "Standard Chartered Hong Kong и Bank of China выпускают HK$20, HK$50, HK$100, "
                    "HK$500 и HK$1,000."
                ),
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_emitters"],
            },
            {
                "atom_id": "atom_ci",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Обеспечение сертификатами",
                "claim": (
                    "HKMA использует Exchange Fund и Certificates of Indebtedness для обеспечения "
                    "выпуска банкнот."
                ),
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_ci"],
            },
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Ограничение LERS",
                "claim": "Выпуск банкнот работает внутри LERS/currency board с курсом 7.80.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_lers"],
            },
            {
                "atom_id": "atom_hk10",
                "job_id": "brief_02",
                "kind": "exception",
                "title": "Эпизод HK$10",
                "claim": (
                    "После попытки заменить HK$10 монетами HKMA выпустила собственные HK$10 notes "
                    "в ответ на общественный спрос на сохранение бумажной купюры."
                ),
                "explanation": (
                    "Это сохраняет полезный эпизод public demand, но не закрывает остаточный вопрос, "
                    "почему эмитентом остаётся HKMA/правительство, а не частные банки."
                ),
                "story_value": "",
                "evidence_ids": ["ev_hk10"],
            },
        ],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": "q_hkd_mech_r4",
                "category": "open_requirement",
                "status": "deferred",
                "question": "Why HKMA/government, rather than private banks, remains the HK$10 issuer.",
                "resolution_note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
            }
        ],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev_emitters": "The HKMA issues the 10-dollar note and the other three banks issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations: The Hong Kong Shanghai Banking Corporation, Standard Chartered Hong Kong and Bank of China.",
            "ev_ci": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev_lers": "Banknote issuance operates under the LERS currency board at 7.80.",
            "ev_hk10": "Between 1994 and 2002 an attempt was made to replace privately issued HK$10 notes with coins. In response to public demand for the continuation of this note, the HKMA issued its own HK$10 notes.",
        },
    )

    assert report["passed"] is True


def test_leaf_research_eval_rejects_requirement_atoms_with_swapped_evidence() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "HK$10 residual remains open.", "evidence_ids": []}],
        "requirement_outcomes": [
            {"requirement_id": "q_hkd_mech_r1", "status": "answered", "note": "Answered.", "evidence_ids": ["ev_emitters"]},
            {"requirement_id": "q_hkd_mech_r2", "status": "answered", "note": "Answered.", "evidence_ids": ["ev_ci"]},
            {"requirement_id": "q_hkd_mech_r3", "status": "answered", "note": "Answered.", "evidence_ids": ["ev_lers"]},
            {
                "requirement_id": "q_hkd_mech_r4",
                "status": "deferred",
                "note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
                "evidence_ids": [],
            },
        ],
        "evidence_ids": ["ev_emitters", "ev_ci", "ev_lers"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev_emitters"]}],
        "atom_ids": ["atom_emitters", "atom_ci", "atom_lers"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "brief_02",
                "kind": "context",
                "title": "Current issuers",
                "claim": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_ci"],
            },
            {
                "atom_id": "atom_ci",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Certificate backing",
                "claim": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back banknote issuance.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_emitters"],
            },
            {
                "atom_id": "atom_lers",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "LERS constraint",
                "claim": "Banknote issuance operates under the LERS currency board at 7.80.",
                "explanation": "",
                "story_value": "",
                "evidence_ids": ["ev_lers"],
            },
        ],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": "q_hkd_mech_r4",
                "category": "open_requirement",
                "status": "deferred",
                "question": "Why HK$10 remains government-issued.",
                "resolution_note": "HK$10: не объяснено, почему HKMA/правительство, а не частные банки, осталось эмитентом.",
            }
        ],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev_emitters": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
            "ev_ci": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev_lers": "Banknote issuance operates under the LERS currency board at 7.80.",
        },
    )

    assert report["passed"] is False
    assert any(
        "q_hkd_mech_r1 answered outcome needs an authored atom using the same evidence" in issue
        for issue in report["issues"]
    )
    assert any(
        "q_hkd_mech_r2 answered outcome needs an authored atom using the same evidence" in issue
        for issue in report["issues"]
    )


def test_leaf_research_eval_rejects_keyword_only_hk_canary_without_evidence_backed_material() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "mapped",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "answered", "note": "Answered.", "evidence_ids": ["ev_noise"]}],
        "requirement_outcomes": [
            {
                "requirement_id": "q_hkd_mech_r1",
                "status": "answered",
                "note": "Гонконгские банкноты: Standard Chartered, Bank of China.",
                "evidence_ids": ["ev_noise"],
            },
            {
                "requirement_id": "q_hkd_mech_r2",
                "status": "answered",
                "note": "HKMA Exchange Fund.",
                "evidence_ids": ["ev_noise"],
            },
            {
                "requirement_id": "q_hkd_mech_r3",
                "status": "answered",
                "note": "LERS 7.80.",
                "evidence_ids": ["ev_noise"],
            },
            {
                "requirement_id": "q_hkd_mech_r4",
                "status": "answered",
                "note": "HK$10.",
                "evidence_ids": ["ev_noise"],
            },
        ],
        "evidence_ids": ["ev_noise"],
        "source_findings": [
            {
                "finding_id": "noise",
                "statement": "This evidence is about an unrelated archival note.",
                "support_explanation": "",
                "evidence_ids": ["ev_noise"],
            }
        ],
        "observations": [
            {
                "note": "HSBC, Standard Chartered, Bank of China, HKMA, Exchange Fund, LERS, 7.80 and HK$10.",
                "source_refs": ["S99"],
                "evidence_ids": ["ev_noise"],
            }
        ],
        "atom_ids": [],
    }

    report = _leaf_job_structural_report(
        {"atom_drafts": [], "research_gaps": []},
        job,
        evidence_text_by_id={"ev_noise": "This evidence is about an unrelated archival note."},
    )

    assert report["passed"] is False
    assert any("q_hkd_mech_r1 evidence-backed material is missing expected domain terms" in issue for issue in report["issues"])
    assert any(
        "q_hkd_mech_r4 evidence-backed material is missing expected residual HK$10 terms" in issue
        for issue in report["issues"]
    )


def test_leaf_research_eval_rejects_r4_gap_terms_copied_only_from_question() -> None:
    from app.generation_v2.research_leaf_eval_runner import _leaf_job_structural_report

    job = {
        "job_id": "brief_02",
        "status": "deferred",
        "questions": ["How are Hong Kong banknotes issued now?"],
        "coverage_question_ids": ["q_hkd_current_mechanics"],
        "coverage_requirements": [
            {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
            {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
            {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
            {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
        ],
        "question_outcomes": [{"question": "How are Hong Kong banknotes issued now?", "status": "deferred", "note": "Deferred.", "evidence_ids": []}],
        "requirement_outcomes": [
            {"requirement_id": "q_hkd_mech_r1", "status": "answered", "note": "Answered.", "evidence_ids": ["ev1"]},
            {"requirement_id": "q_hkd_mech_r2", "status": "answered", "note": "Answered.", "evidence_ids": ["ev2"]},
            {"requirement_id": "q_hkd_mech_r3", "status": "answered", "note": "Answered.", "evidence_ids": ["ev3"]},
            {"requirement_id": "q_hkd_mech_r4", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
        ],
        "evidence_ids": ["ev1", "ev2", "ev3"],
        "observations": [{"note": "Research progress.", "source_refs": ["S03"], "evidence_ids": ["ev1"]}],
        "atom_ids": ["atom_mech"],
    }
    workspace = {
        "atom_drafts": [
            {
                "atom_id": "atom_mech",
                "job_id": "brief_02",
                "kind": "mechanism",
                "title": "Current Hong Kong banknote issuance mechanism",
                "claim": "HSBC, Standard Chartered and Bank of China issue denominations through an HKMA / Exchange Fund Certificate of Indebtedness mechanism inside the LERS currency board at 7.80.",
                "explanation": "The mechanism links private banknote issuance, Certificates of Indebtedness, the Exchange Fund and LERS.",
                "story_value": "",
                "evidence_ids": ["ev1", "ev2", "ev3"],
            }
        ],
        "research_gaps": [
            {
                "job_id": "brief_02",
                "requirement_id": "q_hkd_mech_r4",
                "category": "open_requirement",
                "status": "deferred",
                "question": "Why HK$10 remains government-issued.",
                "resolution_note": "Не нашел.",
            }
        ],
    }

    report = _leaf_job_structural_report(
        workspace,
        job,
        evidence_text_by_id={
            "ev1": "HSBC, Standard Chartered and Bank of China issue HK$20, HK$50, HK$100, HK$500 and HK$1,000 denominations.",
            "ev2": "HKMA uses the Exchange Fund and Certificates of Indebtedness to back note issuance.",
            "ev3": "Banknote issuance operates under the LERS currency board at 7.80.",
        },
    )

    assert report["passed"] is False
    assert any(
        "q_hkd_mech_r4 deferred outcome/gap text is missing expected residual HK$10 terms" in issue
        for issue in report["issues"]
    )


@pytest.mark.asyncio
async def test_research_leaf_eval_marks_eval_run_completed_on_green_report(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.generation_v2.research_leaf_eval_runner as runner

    run_id = uuid.uuid4()
    original_workspace = {
        "investigation_jobs": [
            {
                "job_id": "brief_01",
                "status": "planned",
                "coverage_question_ids": ["q_other"],
                "coverage_requirements": [],
            }
        ],
        "atom_drafts": [],
        "research_gaps": [],
    }
    green_workspace = deepcopy(original_workspace)
    green_workspace["investigation_jobs"][0].update(
        {
            "status": "mapped",
            "questions": ["Other question"],
            "question_outcomes": [{"question": "Other question", "status": "answered", "note": "Answered.", "evidence_ids": []}],
            "requirement_outcomes": [],
            "observations": [{"note": "Progress.", "source_refs": ["S01"], "evidence_ids": []}],
            "atom_ids": [],
            "evidence_ids": [],
        }
    )
    saved: dict[str, object] = {}
    run_record = SimpleNamespace(id=run_id)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _run_id):
            return run_record

        async def commit(self):
            saved["committed"] = True

    async def preflight(**_kwargs):
        return {"ready": True, "issues": []}

    async def load_state(_db, _run):
        return SimpleNamespace(
            corpus_brief={"ok": True},
            corpus_brief_artifact_id=uuid.uuid4(),
            source_map=SourceMap(),
            source_inventory=SourceMap(),
            corpus=SimpleNamespace(run_view={}),
            evidence_tools=[],
            evidence_toolset=None,
        )

    async def ensure_workspace(*_args, **_kwargs):
        return deepcopy(original_workspace)

    async def run_job(**_kwargs):
        return deepcopy(green_workspace)

    async def save_workspace(_db, *, workspace, **_kwargs):
        saved["workspace"] = deepcopy(workspace)
        return SimpleNamespace(payload=workspace)

    async def costs(_db, _run_id):
        return {"totals": {"estimated_token_cost_usd": 0.2, "max_call_cost_usd": 0.1}}

    async def evidence_texts(*_args, **_kwargs):
        return {}

    async def clone_run(**_kwargs):
        return SimpleNamespace(id=run_id)

    async def trace_metrics(_db, _run_id):
        return {
            "observed_models": {"requested": [], "response": [], "billing": []},
            "model_call_count": 0,
            "latency_ms": {"total": 0.0, "max": 0.0, "average": 0.0},
        }

    monkeypatch.setattr(runner, "async_session_factory", lambda: FakeSession())
    monkeypatch.setattr(runner, "preflight_leaf_research_eval", preflight)
    monkeypatch.setattr(runner, "_clone_research_leaf_eval_run", clone_run)
    monkeypatch.setattr(runner, "load_passed_triage_state", load_state)
    monkeypatch.setattr(runner, "ensure_research_workspace", ensure_workspace)
    monkeypatch.setattr(runner, "run_research_job", run_job)
    monkeypatch.setattr(runner, "save_research_workspace", save_workspace)
    monkeypatch.setattr(runner, "summarize_tool_trace_costs", costs)
    monkeypatch.setattr(runner, "_load_evidence_text_by_id", evidence_texts)
    monkeypatch.setattr(runner, "_research_leaf_trace_metrics", trace_metrics)

    result = await runner.run_leaf_research_eval(
        triage_run_id=run_id,
        question_id="q_other",
        max_cost_usd=1.0,
    )

    assert result["structural_report"]["passed"] is True
    assert saved["workspace"] == green_workspace
    assert saved["committed"] is True
    assert run_record.status == runner.GenerationStatus.COMPLETED
    assert run_record.actual_cost == {"totals": {"estimated_token_cost_usd": 0.2, "max_call_cost_usd": 0.1}}
    assert run_record.completed_at is not None
    assert run_record.error_message is None


@pytest.mark.asyncio
async def test_research_leaf_eval_marks_eval_run_failed_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.generation_v2.research_leaf_eval_runner as runner

    run_id = uuid.uuid4()
    run_record = SimpleNamespace(id=run_id)
    committed: dict[str, bool] = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _run_id):
            return run_record

        async def commit(self):
            committed["value"] = True

    async def preflight(**_kwargs):
        return {"ready": True, "issues": []}

    async def clone_run(**_kwargs):
        return SimpleNamespace(id=run_id)

    async def execute(**_kwargs):
        raise RuntimeError("provider exploded")

    async def costs(_db, _run_id):
        return {"totals": {"estimated_token_cost_usd": 0.3, "max_call_cost_usd": 0.1}}

    monkeypatch.setattr(runner, "async_session_factory", lambda: FakeSession())
    monkeypatch.setattr(runner, "preflight_leaf_research_eval", preflight)
    monkeypatch.setattr(runner, "_clone_research_leaf_eval_run", clone_run)
    monkeypatch.setattr(runner, "_execute_leaf_research_eval_run", execute)
    monkeypatch.setattr(runner, "summarize_tool_trace_costs", costs)

    with pytest.raises(RuntimeError, match="provider exploded"):
        await runner.run_leaf_research_eval(
            triage_run_id=run_id,
            question_id="q_other",
            max_cost_usd=1.0,
        )

    assert committed["value"] is True
    assert run_record.status == runner.GenerationStatus.FAILED
    assert run_record.actual_cost == {"totals": {"estimated_token_cost_usd": 0.3, "max_call_cost_usd": 0.1}}
    assert run_record.completed_at is not None
    assert run_record.error_message == "provider exploded"


@pytest.mark.asyncio
async def test_research_leaf_eval_restores_original_workspace_after_red_report(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.generation_v2.research_leaf_eval_runner as runner

    run_id = uuid.uuid4()
    original_workspace = {
        "investigation_jobs": [
            {
                "job_id": "brief_02",
                "status": "planned",
                "coverage_question_ids": ["q_hkd_current_mechanics"],
                "coverage_requirements": [
                    {"requirement_id": "q_hkd_mech_r1", "description": "emitters"},
                    {"requirement_id": "q_hkd_mech_r2", "description": "HKMA / Exchange Fund"},
                    {"requirement_id": "q_hkd_mech_r3", "description": "LERS"},
                    {"requirement_id": "q_hkd_mech_r4", "description": "HK$10 exception"},
                ],
            }
        ],
        "atom_drafts": [],
        "research_gaps": [],
    }
    red_workspace = deepcopy(original_workspace)
    red_workspace["investigation_jobs"][0].update(
        {
            "status": "deferred",
            "questions": ["How are Hong Kong banknotes issued now?"],
            "question_outcomes": [
                {
                    "question": "How are Hong Kong banknotes issued now?",
                    "status": "deferred",
                    "note": "Deferred.",
                    "evidence_ids": [],
                }
            ],
            "requirement_outcomes": [
                {"requirement_id": "q_hkd_mech_r1", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
                {"requirement_id": "q_hkd_mech_r2", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
                {"requirement_id": "q_hkd_mech_r3", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
                {"requirement_id": "q_hkd_mech_r4", "status": "deferred", "note": "Deferred.", "evidence_ids": []},
            ],
            "observations": [],
            "atom_ids": [],
            "evidence_ids": [],
        }
    )
    saved: dict[str, object] = {}
    run_record = SimpleNamespace(id=run_id)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _run_id):
            return run_record

        async def commit(self):
            saved["committed"] = True

    async def preflight(**_kwargs):
        return {"ready": True, "issues": []}

    async def load_state(_db, _run):
        return SimpleNamespace(
            corpus_brief={"ok": True},
            corpus_brief_artifact_id=uuid.uuid4(),
            source_map=SourceMap(),
            source_inventory=SourceMap(),
            corpus=SimpleNamespace(run_view={}),
            evidence_tools=[],
            evidence_toolset=None,
        )

    async def ensure_workspace(*_args, **_kwargs):
        return deepcopy(original_workspace)

    async def run_job(**_kwargs):
        return deepcopy(red_workspace)

    async def save_workspace(_db, *, workspace, **_kwargs):
        saved["workspace"] = deepcopy(workspace)
        return SimpleNamespace(payload=workspace)

    async def costs(_db, _run_id):
        return {"totals": {"estimated_token_cost_usd": 0.0, "max_call_cost_usd": 0.0}}

    async def evidence_texts(*_args, **_kwargs):
        return {}

    async def clone_run(**_kwargs):
        return SimpleNamespace(id=run_id)

    async def trace_metrics(_db, _run_id):
        return {
            "observed_models": {"requested": [], "response": [], "billing": []},
            "model_call_count": 0,
            "latency_ms": {"total": 0.0, "max": 0.0, "average": 0.0},
        }

    monkeypatch.setattr(runner, "async_session_factory", lambda: FakeSession())
    monkeypatch.setattr(runner, "preflight_leaf_research_eval", preflight)
    monkeypatch.setattr(runner, "_clone_research_leaf_eval_run", clone_run)
    monkeypatch.setattr(runner, "load_passed_triage_state", load_state)
    monkeypatch.setattr(runner, "ensure_research_workspace", ensure_workspace)
    monkeypatch.setattr(runner, "run_research_job", run_job)
    monkeypatch.setattr(runner, "save_research_workspace", save_workspace)
    monkeypatch.setattr(runner, "summarize_tool_trace_costs", costs)
    monkeypatch.setattr(runner, "_load_evidence_text_by_id", evidence_texts)
    monkeypatch.setattr(runner, "_research_leaf_trace_metrics", trace_metrics)

    with pytest.raises(RuntimeError, match="Research leaf eval failed"):
        await runner.run_leaf_research_eval(
            triage_run_id=run_id,
            question_id="q_hkd_current_mechanics",
            max_cost_usd=1.0,
        )

    assert saved["workspace"] == original_workspace
    assert saved["committed"] is True
    assert run_record.status == runner.GenerationStatus.FAILED
    assert run_record.actual_cost == {"totals": {"estimated_token_cost_usd": 0.0, "max_call_cost_usd": 0.0}}
    assert run_record.completed_at is not None
    assert str(run_record.error_message).startswith("Research leaf eval failed:")


@pytest.mark.asyncio
async def test_research_leaf_eval_cli_exits_nonzero_on_red_report(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import app.generation_v2.research_leaf_eval_runner as runner

    async def red_run(**_kwargs):
        return {"structural_report": {"passed": False}}

    monkeypatch.setattr(runner, "run_leaf_research_eval", red_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "research_leaf_eval_runner",
            "--triage-run-id",
            str(uuid.uuid4()),
            "--question-id",
            "q_hkd_current_mechanics",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await runner._main_async()
    assert exc.value.code == 1


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
                "priority": "critical",
                "source_refs": ["S01"],
                "questions": ["How?", "Why?"],
                # Planner hints guide the agent; they must not become a semantic completion gate.
                "required_atom_kinds": ["mechanism"],
                "observations": [{"note": "Finding", "source_refs": ["S01"], "evidence_ids": [evidence_id]}],
                "atom_ids": ["atom_emitters", "atom_hk10"],
                "evidence_ids": [evidence_id],
            }
        ],
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "job_1",
                "kind": "context",
                "title": "Emitters",
                "claim": "Evidence identifies the emitters.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
            },
            {
                "atom_id": "atom_hk10",
                "job_id": "job_1",
                "kind": "exception",
                "title": "HK$10 exception",
                "claim": "Evidence explains why HK$10 stays exceptional.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
            },
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
                {
                    "question": "How?",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                }
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
                {
                    "question": "How?",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                },
                {"question": "Why?", "status": "deferred", "note": "No source answers it.", "evidence_ids": []},
            ],
        }
    )

    assert rejected["accepted"] is False
    assert rejected["missing_questions"] == ["Why?"]
    assert accepted["accepted"] is True
    assert workspace["investigation_jobs"][0]["question_outcomes"][1]["status"] == "deferred"


@pytest.mark.asyncio
async def test_research_job_pages_source_findings_by_requirement_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_v2.research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "in_progress",
                "coverage_requirements": [
                    {
                        "question_id": "q_current",
                        "requirement_id": "req_hk10",
                        "description": "Explain the HK$10 exception and public demand.",
                    }
                ],
                "source_findings": [
                    {
                        "finding_id": "f_noise",
                        "source_ref": "S01",
                        "statement": "The linked exchange rate system uses certificates of indebtedness.",
                        "evidence_ids": ["ev_lers"],
                        "question_ids": ["q_current"],
                    },
                    {
                        "finding_id": "f_hk10",
                        "source_ref": "S03",
                        "statement": "Public demand led HKMA to issue HK$10 notes.",
                        "evidence_ids": ["ev_hk10"],
                        "question_ids": ["q_current"],
                    },
                ],
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    page_tool = next(
        tool
        for tool in build_research_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=run,  # type: ignore[arg-type]
            workspace=workspace,
            source_inventory=SourceMap(),
            mode="job",
            current_job_id="job_1",
        )
        if tool.name == "list_research_workspace_items"
    )

    page = await page_tool.handler(
        {
            "section": "current_job_source_findings",
            "cursor": "0",
            "limit": 1,
            "requirement_id": "req_hk10",
            "query": "HK$10 public demand",
        }
    )

    assert page["accepted"] is True
    assert page["items"][0]["finding_id"] == "f_hk10"
    assert page["items"][0]["evidence_ids"] == ["ev_hk10"]
    assert page["page"]["total_count"] == 1


@pytest.mark.asyncio
async def test_finish_research_job_requires_requirement_outcomes_and_records_deferred_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_v2.research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "research_gaps": [],
        "atom_drafts": [
            {
                "atom_id": "atom_emitters",
                "job_id": "job_1",
                "kind": "mechanism",
                "title": "Emitters",
                "claim": "Evidence identifies the emitters.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
            },
            {
                "atom_id": "atom_hk10",
                "job_id": "job_1",
                "kind": "mechanism",
                "title": "HK$10",
                "claim": "Evidence explains why HK$10 is exceptional.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
            },
        ],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "in_progress",
                "priority": "critical",
                "source_refs": ["S01"],
                "questions": ["How does it work?"],
                "coverage_requirements": [
                    {
                        "question_id": "q_current",
                        "requirement_id": "r_emitters",
                        "description": "Identify the emitters.",
                    },
                    {
                        "question_id": "q_current",
                        "requirement_id": "r_hk10_why",
                        "description": "Explain why HK$10 is exceptional.",
                    },
                ],
                "observations": [{"note": "Finding", "source_refs": ["S01"], "evidence_ids": [evidence_id]}],
                "atom_ids": ["atom_emitters", "atom_hk10"],
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
    finish = next(
        tool
        for tool in build_research_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=run,  # type: ignore[arg-type]
            workspace=workspace,
            source_inventory=SourceMap(),
            mode="job",
            current_job_id="job_1",
        )
        if tool.name == "finish_research_job"
    )

    missing = await finish.handler(
        {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped.",
            "deferred_reason": "",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_emitters",
                    "status": "answered",
                    "note": "Evidence identifies the emitters.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_emitters"],
                }
            ],
        }
    )
    mapped_with_deferred_requirement = await finish.handler(
        {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped with one open requirement.",
            "deferred_reason": "",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_emitters",
                    "status": "answered",
                    "note": "Evidence identifies the emitters.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_emitters"],
                },
                {
                    "requirement_id": "r_hk10_why",
                    "status": "deferred",
                    "note": "No source explains why HK$10 stays exceptional.",
                    "evidence_ids": [],
                    "supporting_atom_ids": [],
                },
            ],
        }
    )
    answered_question_with_deferred_requirement = await finish.handler(
        {
            "job_id": "job_1",
            "status": "deferred",
            "summary": "Deferred with one open requirement.",
            "deferred_reason": "One coverage requirement remains unresolved.",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "answered",
                    "note": "Evidence answers only part of it.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_emitters",
                    "status": "answered",
                    "note": "Evidence identifies the emitters.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_emitters"],
                },
                {
                    "requirement_id": "r_hk10_why",
                    "status": "deferred",
                    "note": "No source explains why HK$10 stays exceptional.",
                    "evidence_ids": [],
                    "supporting_atom_ids": [],
                },
            ],
        }
    )
    deferred_question_with_answered_requirements = await finish.handler(
        {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped with all requirements answered.",
            "deferred_reason": "",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "deferred",
                    "note": "Incorrectly deferred despite all requirements being answered.",
                    "evidence_ids": [],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_emitters",
                    "status": "answered",
                    "note": "Evidence identifies the emitters.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_emitters"],
                },
                {
                    "requirement_id": "r_hk10_why",
                    "status": "answered",
                    "note": "Evidence explains why HK$10 stays exceptional.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_hk10"],
                },
            ],
        }
    )
    accepted = await finish.handler(
        {
            "job_id": "job_1",
            "status": "deferred",
            "summary": "Deferred with one open requirement.",
            "deferred_reason": "One coverage requirement remains unresolved.",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "deferred",
                    "note": "One required part remains unresolved.",
                    "evidence_ids": [],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_emitters",
                    "status": "answered",
                    "note": "Evidence identifies the emitters.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_emitters"],
                },
                {
                    "requirement_id": "r_hk10_why",
                    "status": "deferred",
                    "note": "No source explains why HK$10 stays exceptional.",
                    "evidence_ids": [],
                    "supporting_atom_ids": [],
                },
            ],
        }
    )
    assert missing["accepted"] is False
    assert missing["missing_requirement_ids"] == ["r_hk10_why"]
    assert mapped_with_deferred_requirement["accepted"] is False
    assert mapped_with_deferred_requirement["deferred_requirement_ids"] == ["r_hk10_why"]
    assert answered_question_with_deferred_requirement["accepted"] is False
    assert answered_question_with_deferred_requirement["answered_questions_with_deferred_requirements"] == [
        "How does it work?"
    ]
    assert deferred_question_with_answered_requirements["accepted"] is False
    assert deferred_question_with_answered_requirements["deferred_questions_with_answered_requirements"] == [
        "How does it work?"
    ]
    assert accepted["accepted"] is True
    job = workspace["investigation_jobs"][0]
    assert [item["requirement_id"] for item in job["requirement_outcomes"]] == [
        "r_emitters",
        "r_hk10_why",
    ]
    assert [item["requirement_id"] for item in job["open_requirements"]] == ["r_hk10_why"]
    assert job["coverage_requirements"][0]["result"]["supporting_atom_ids"] == ["atom_emitters"]
    assert workspace["research_gaps"][0]["question"] == "Explain why HK$10 is exceptional."
    assert workspace["research_gaps"][0]["category"] == "open_requirement"
    assert workspace["research_gaps"][0]["treatment_relevance"] == "central"
    assert workspace["research_gaps"][0]["requirement_id"] == "r_hk10_why"
    assert workspace["research_gaps"][0]["status"] == "deferred"
    resolved = await finish.handler(
        {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped with both requirements answered.",
            "deferred_reason": "",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_emitters",
                    "status": "answered",
                    "note": "Evidence identifies the emitters.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_emitters"],
                },
                {
                    "requirement_id": "r_hk10_why",
                    "status": "answered",
                    "note": "Evidence now explains why HK$10 stays exceptional.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": ["atom_hk10"],
                },
            ],
        }
    )
    assert resolved["accepted"] is True
    assert workspace["research_gaps"][0]["status"] == "resolved"
    assert workspace["research_gaps"][0]["evidence_ids"] == [evidence_id]


@pytest.mark.asyncio
async def test_finish_research_job_validates_answered_requirement_supporting_atoms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_v2.research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    other_evidence_id = str(uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "research_gaps": [],
        "atom_drafts": [
            {
                "atom_id": "atom_good",
                "job_id": "job_1",
                "kind": "mechanism",
                "title": "Mechanism",
                "claim": "Evidence answers the requirement.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
            },
            {
                "atom_id": "atom_other_evidence",
                "job_id": "job_1",
                "kind": "mechanism",
                "title": "Different evidence",
                "claim": "Different evidence supports a different claim.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [other_evidence_id],
            },
            {
                "atom_id": "atom_other_job",
                "job_id": "job_2",
                "kind": "mechanism",
                "title": "Wrong job",
                "claim": "This belongs to another job.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
            },
            {
                "atom_id": "atom_merged",
                "job_id": "job_1",
                "kind": "mechanism",
                "title": "Merged",
                "claim": "Inactive atom.",
                "explanation": "",
                "story_value": "Useful research material.",
                "evidence_ids": [evidence_id],
                "status": "merged",
            },
        ],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "in_progress",
                "priority": "critical",
                "source_refs": ["S01"],
                "questions": ["How does it work?"],
                "coverage_requirements": [
                    {"question_id": "q_current", "requirement_id": "r_mechanism", "description": "Explain it."}
                ],
                "observations": [{"note": "Finding", "source_refs": ["S01"], "evidence_ids": [evidence_id]}],
                "atom_ids": ["atom_good", "atom_other_evidence", "atom_other_job", "atom_merged"],
                "evidence_ids": [evidence_id, other_evidence_id],
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
    finish = next(
        tool
        for tool in build_research_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=run,  # type: ignore[arg-type]
            workspace=workspace,
            source_inventory=SourceMap(),
            mode="job",
            current_job_id="job_1",
        )
        if tool.name == "finish_research_job"
    )

    def args(supporting_atom_ids: list[str]):
        return {
            "job_id": "job_1",
            "status": "mapped",
            "summary": "Mapped.",
            "deferred_reason": "",
            "question_outcomes": [
                {
                    "question": "How does it work?",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "requirement_outcomes": [
                {
                    "requirement_id": "r_mechanism",
                    "status": "answered",
                    "note": "Evidence answers it.",
                    "evidence_ids": [evidence_id],
                    "supporting_atom_ids": supporting_atom_ids,
                }
            ],
        }

    missing = await finish.handler(args([]))
    wrong_job = await finish.handler(args(["atom_other_job"]))
    inactive = await finish.handler(args(["atom_merged"]))
    no_shared_evidence = await finish.handler(args(["atom_other_evidence"]))
    accepted = await finish.handler(args(["atom_good"]))

    assert missing["accepted"] is False
    assert missing["error"] == "answered requirement requires at least one supporting_atom_id"
    assert wrong_job["accepted"] is False
    assert wrong_job["invalid_supporting_atom_ids"] == ["atom_other_job"]
    assert inactive["accepted"] is False
    assert inactive["invalid_supporting_atom_ids"] == ["atom_merged"]
    assert no_shared_evidence["accepted"] is False
    assert no_shared_evidence["supporting_atom_ids_without_shared_evidence"] == ["atom_other_evidence"]
    assert accepted["accepted"] is True
    job = workspace["investigation_jobs"][0]
    assert job["requirement_outcomes"][0]["supporting_atom_ids"] == ["atom_good"]
    assert job["coverage_requirements"][0]["result"]["supporting_atom_ids"] == ["atom_good"]


def test_finish_research_job_schema_does_not_cap_requirement_outcomes() -> None:
    from app.generation_v2.runtime_schema import strict_json_schema

    tool = next(
        tool
        for tool in build_research_workspace_tools(
            db=None,  # type: ignore[arg-type]
            run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            workspace={"investigation_jobs": []},
            source_inventory=SourceMap(),
            mode="job",
            current_job_id="job_1",
        )
        if tool.name == "finish_research_job"
    )
    schema = strict_json_schema(tool.parameters)

    assert "maxItems" not in schema["properties"]["requirement_outcomes"]
    requirement_schema = schema["properties"]["requirement_outcomes"]["items"]
    assert "supporting_atom_ids" in requirement_schema["required"]


@pytest.mark.asyncio
async def test_deferred_requirement_gap_identity_includes_job_and_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_v2.research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "source_order": ["S01", "S02"],
        "research_gaps": [],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "in_progress",
                "priority": "critical",
                "source_refs": ["S01"],
                "questions": ["Q1"],
                "coverage_question_ids": ["q1"],
                "coverage_requirements": [
                    {
                        "question_id": "q1",
                        "requirement_id": "r_same",
                        "description": "Explain the mechanism.",
                    }
                ],
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [],
            },
            {
                "job_id": "job_2",
                "status": "in_progress",
                "priority": "critical",
                "source_refs": ["S02"],
                "questions": ["Q2"],
                "coverage_question_ids": ["q2"],
                "coverage_requirements": [
                    {
                        "question_id": "q2",
                        "requirement_id": "r_same",
                        "description": "Explain the mechanism.",
                    }
                ],
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [],
            },
        ],
        "processed_jobs": [],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)

    async def finish_job(job_id: str, question: str):
        finish = next(
            tool
            for tool in build_research_workspace_tools(
                db=None,  # type: ignore[arg-type]
                run=run,  # type: ignore[arg-type]
                workspace=workspace,
                source_inventory=SourceMap(),
                mode="job",
                current_job_id=job_id,
            )
            if tool.name == "finish_research_job"
        )
        return await finish.handler(
            {
                "job_id": job_id,
                "status": "deferred",
                "summary": "Deferred.",
                "deferred_reason": "Mechanism remains open.",
                "question_outcomes": [
                    {
                        "question": question,
                        "status": "deferred",
                        "note": "Mechanism remains open.",
                        "evidence_ids": [],
                    }
                ],
                "requirement_outcomes": [
                    {
                        "requirement_id": "r_same",
                        "status": "deferred",
                        "note": "Mechanism remains open.",
                        "evidence_ids": [],
                    }
                ],
            }
        )

    assert (await finish_job("job_1", "Q1"))["accepted"] is True
    assert (await finish_job("job_2", "Q2"))["accepted"] is True

    assert len(workspace["research_gaps"]) == 2
    assert {gap["job_id"] for gap in workspace["research_gaps"]} == {"job_1", "job_2"}
    assert len({gap["normalized_key"] for gap in workspace["research_gaps"]}) == 2


def _complete_set_semantic_review(workspace: dict, set_id: str) -> None:
    source_set = next(item for item in workspace["source_sets"] if item["set_id"] == set_id)
    cursor = 0
    while True:
        page = semantic_review_page(
            workspace,
            source_set,
            evidence_by_id={},
            cursor=cursor,
            limit=25,
        )
        record_semantic_review_page(workspace, source_set, page)
        next_cursor = str(page["page"]["next_cursor"] or "")
        if not next_cursor:
            break
        cursor = int(next_cursor)
    workspace.setdefault("source_triage_set_semantic_self_checks", {})[set_id] = {
        "contract_version": "v2.semantic_review_ack.1",
        "review_snapshot_id": set_semantic_review_signature(workspace, source_set),
        "readiness_explanation": "Reviewed in the test helper.",
    }


@pytest.mark.asyncio
async def test_mapped_triage_set_requires_one_resolved_assessment_per_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    source_1 = str(uuid.uuid4())
    source_2 = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            _source(source_id=source_1, title="First"),
            _source(source_id=source_2, title="Second"),
        ]
    )
    workspace = {
        "source_order": ["S01", "S02"],
        "current_set": {"set_id": "set_1"},
        "source_sets": [
            {
                "set_id": "set_1",
                "status": "in_progress",
                "source_refs": ["S01", "S02"],
                "question_ids": ["q1"],
                "observations": [],
                "written_actions": [],
                "evidence_ids": [],
            }
        ],
        "source_assessments": [
            {
                "set_id": "set_1",
                "source_ref": "S01",
                "source_id": source_1,
                "processing_status": "resolved",
                "representation_status": "readable",
                "disposition": "anchor",
                "relevance_reason": "The source is active.",
                "no_findings_reason": "The completed reading yielded no substantive finding.",
            }
        ],
        "coverage_outcomes": [{"set_id": "set_1", "question_id": "q1", "status": "covered"}],
        "repair_baseline_source_assessments": [
            {"set_id": "set_1", "source_ref": "S01", "repair_baseline": True},
            {"set_id": "set_other", "source_ref": "S99", "repair_baseline": True},
        ],
        "repair_baseline_coverage_outcomes": [
            {"set_id": "set_1", "question_id": "q1", "repair_baseline": True},
            {"set_id": "set_other", "question_id": "q99", "repair_baseline": True},
        ],
        "processed_sets": [],
        "mapped_sources": [],
        "deferred_sources": [],
    }
    workspace["coverage_outcomes"][0]["workbench_snapshot_id"] = question_workbench_signature(
        workspace,
        workspace["source_sets"][0],
        question_id="q1",
    )

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
        inventory=inventory,
        mode="set",
        current_set_id="set_1",
    )
    finish = next(tool for tool in tools if tool.name == "finish_triage_source_set")

    rejected = await finish.handler(
        {"set_id": "set_1", "status": "mapped", "summary": "Partial.", "deferred_reason": "", "deferred_sources": []}
    )
    assert len(workspace["repair_baseline_source_assessments"]) == 2
    assert len(workspace["repair_baseline_coverage_outcomes"]) == 2
    rejected_repair_status = await finish.handler(
        {
            "set_id": "set_1",
            "status": "needs_repair",
            "summary": "The second source still needs work.",
            "deferred_reason": "The set is not complete.",
            "deferred_sources": [],
        }
    )
    assert rejected_repair_status["accepted"] is False
    assert rejected_repair_status["error"] == "status must be mapped or deferred"
    assert len(workspace["repair_baseline_source_assessments"]) == 2
    assert len(workspace["repair_baseline_coverage_outcomes"]) == 2
    workspace["source_assessments"].append(
        {
            "set_id": "set_1",
            "source_ref": "S02",
            "source_id": source_2,
            "processing_status": "resolved",
            "representation_status": "unreadable",
            "disposition": "deferred",
            "relevance_reason": "The source is explicitly deferred.",
            "no_findings_reason": "No inspectable source representation was available.",
        }
    )
    contradictory_source_deferral = await finish.handler(
        {
            "set_id": "set_1",
            "status": "mapped",
            "summary": "Contradictory source deferral.",
            "deferred_reason": "",
            "deferred_sources": [{"source_ref": "S01", "reason": "Requested only at finish."}],
        }
    )
    contradictory_set_deferral = await finish.handler(
        {
            "set_id": "set_1",
            "status": "deferred",
            "summary": "Contradictory set deferral.",
            "deferred_reason": "The whole set is deferred.",
            "deferred_sources": [],
        }
    )
    _complete_set_semantic_review(workspace, "set_1")
    accepted = await finish.handler(
        {
            "set_id": "set_1",
            "status": "mapped",
            "summary": "Mapped S01; bounded S02.",
            "deferred_reason": "",
            "deferred_sources": [],
        }
    )

    assert rejected["missing_source_refs"] == ["S02"]
    assert contradictory_source_deferral["accepted"] is False
    assert contradictory_source_deferral["source_refs"] == ["S01"]
    assert contradictory_set_deferral["accepted"] is False
    assert contradictory_set_deferral["source_refs"] == ["S01"]
    assert accepted["accepted"] is True
    assert workspace["mapped_sources"] == ["S01"]
    assert workspace["deferred_sources"] == [{"source_ref": "S02", "reason": "The source is explicitly deferred."}]
    assert workspace["source_sets"][0]["assessment_source_refs"] == ["S01"]
    assert workspace["source_sets"][0]["deferred_source_refs"] == ["S02"]
    assert workspace["repair_baseline_source_assessments"] == [
        {"set_id": "set_other", "source_ref": "S99", "repair_baseline": True}
    ]
    assert workspace["repair_baseline_coverage_outcomes"] == [
        {"set_id": "set_other", "question_id": "q99", "repair_baseline": True}
    ]

    workspace.pop("repair_baseline_source_assessments")
    workspace.pop("repair_baseline_coverage_outcomes")
    workspace["source_assessments"][0]["disposition"] = "deferred"
    workspace["source_assessments"][0]["relevance_reason"] = "The whole set is now deferred."
    workspace["coverage_outcomes"][0].update({"status": "deferred", "deferred_reason": "The question is deferred."})
    _complete_set_semantic_review(workspace, "set_1")
    deferred = await finish.handler(
        {
            "set_id": "set_1",
            "status": "deferred",
            "summary": "The full set is explicitly deferred.",
            "deferred_reason": "The full set cannot be used in this run.",
            "deferred_sources": [],
        }
    )

    assert deferred["accepted"] is True
    assert workspace["mapped_sources"] == []
    assert workspace["deferred_sources"] == [
        {"source_ref": "S01", "reason": "The full set cannot be used in this run."},
        {"source_ref": "S02", "reason": "The full set cannot be used in this run."},
    ]
    assert "repair_baseline_source_assessments" not in workspace
    assert "repair_baseline_coverage_outcomes" not in workspace


@pytest.mark.asyncio
async def test_triage_planner_cannot_defer_sources(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "error": "The planner cannot defer sources; assign every source to exactly one set",
    }
    assert workspace["deferred_sources"] == []


def test_model_context_uses_explicit_previews_and_reports_omitted_pages() -> None:
    long_claim = "x" * 900
    workspace = {
        "source_order": ["S01"],
        "atom_drafts": [
            {"atom_id": f"a{idx}", "kind": "mechanism", "title": f"Atom {idx}", "claim": long_claim} for idx in range(3)
        ],
        "investigation_jobs": [],
    }

    payload = research_workspace_summary_for_prompt(workspace, max_atoms=1)

    assert "claim" not in payload["atom_drafts"][0]
    assert payload["atom_drafts"][0]["claim_preview"] == long_claim[:700]
    assert payload["atom_drafts"][0]["claim_truncated"] is True
    assert payload["pages"]["atom_drafts"]["omitted_count"] == 2
    assert payload["pages"]["atom_drafts"]["next_cursor"] == "1"


def test_research_job_context_does_not_copy_foreign_semantic_work() -> None:
    long_foreign_claim = "foreign semantic finding " * 200
    workspace = {
        "source_order": ["S01", "S02"],
        "investigation_jobs": [
            {"job_id": "current", "label": "Current", "source_refs": ["S01"], "status": "in_progress"},
            {"job_id": "done", "label": "Previous", "source_refs": ["S02"], "status": "mapped"},
        ],
        "atom_drafts": [
            {
                "atom_id": f"foreign-{idx}",
                "job_id": "done",
                "kind": "mechanism",
                "title": f"Foreign {idx}",
                "claim": long_foreign_claim,
                "explanation": long_foreign_claim,
                "story_value": long_foreign_claim,
                "source_refs": ["S02"],
            }
            for idx in range(80)
        ],
    }
    payload = research_job_prompt_payload(workspace, "current", source_map_summary={})
    encoded = json.dumps(payload, ensure_ascii=False)

    assert "workspace_progress" not in payload
    assert "other_job_index" not in encoded
    assert "foreign semantic finding" not in encoded
    assert len(encoded) < 8000


def test_research_finalizer_uses_compact_atom_index_without_initial_observation_dump() -> None:
    workspace = {
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "job",
                "label": "Job",
                "status": "mapped",
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "note": "long observation " * 1000,
                        "evidence_ids": ["hidden-evidence"],
                    }
                ],
            }
        ],
        "atom_drafts": [
            {
                "atom_id": f"atom-{idx}",
                "job_id": "job",
                "kind": "mechanism",
                "title": f"Atom {idx}",
                "claim": "specific claim " * 100,
                "explanation": "full explanation " * 300,
                "story_value": "story value " * 100,
                "source_refs": ["S01"],
                "evidence_ids": ["hidden-evidence"],
            }
            for idx in range(3)
        ],
    }

    payload = research_finalizer_payload_for_prompt(workspace)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert len(payload["atom_index"]) == 3
    assert payload["observations"] == []
    assert payload["pages"]["observations"]["total_count"] == 1
    assert "full explanation" not in encoded
    assert "long observation" not in encoded
    assert "hidden-evidence" not in encoded


def test_research_planner_gets_routing_index_not_quote_or_summary_dump() -> None:
    cards = [
        _source(title=f"Source {idx}").model_copy(
            update={"summary": "full source summary " * 200, "gaps_or_cautions": ["warning " * 200]}
        )
        for idx in range(100)
    ]
    source_map = SourceMap(source_cards=cards)

    payload = source_map_planner_summary(source_map, source_map)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["source_card_count"] == 100
    assert len(payload["source_index"]) == 40
    assert payload["pages"]["source_map_cards"]["omitted_count"] == 60
    assert "full source summary" not in encoded
    assert "warning" not in encoded
    assert len(encoded) < 30000


def test_research_job_source_map_summary_metadata_matches_omitted_catalog() -> None:
    cards = [_source(title=f"Source {idx}") for idx in range(3)]
    source_map = SourceMap(source_cards=cards)
    summary = source_map_research_summary(source_map, source_map)
    workspace = {
        "source_order": ["S01", "S02", "S03"],
        "investigation_jobs": [{"job_id": "job_1", "source_refs": ["S01"], "status": "in_progress"}],
    }

    payload = research_job_prompt_payload(workspace, "job_1", source_map_summary=summary)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert "source_map_summary" not in payload
    assert "source_cards" not in encoded
    assert payload["job"]["source_scope"]["allowed_source_pool"]["source_count"] == 1
    assert payload["job"]["source_scope"]["recommended_source_refs"] == ["S01"]


def test_research_job_payload_is_requirement_first_and_omits_lineage_catalogs() -> None:
    evidence_ids = [f"ev-{idx:03d}" for idx in range(98)]
    source_findings = [
        {
            "finding_id": f"finding-{idx:03d}",
            "source_ref": f"S{(idx % 26) + 1:02d}",
            "statement": f"Finding {idx} gives a compact lead for the question.",
            "limitations": [],
            "evidence_ids": [evidence_ids[idx % len(evidence_ids)]],
            "question_ids": ["q_leaf"],
        }
        for idx in range(127)
    ]
    source_gaps = [
        {
            "source_ref": f"S{(idx % 26) + 1:02d}",
            "question_id": "q_leaf",
            "description": f"Residual gap {idx}.",
            "evidence_ids": [evidence_ids[idx % len(evidence_ids)]],
        }
        for idx in range(21)
    ]
    job = {
        "job_id": "brief_02",
        "corpus_handoff_id": "handoff:q_leaf",
        "coverage_question_ids": ["q_leaf"],
        "source_refs": [f"S{idx:02d}" for idx in range(1, 27)],
        "starting_evidence_ids": evidence_ids,
        "source_findings": source_findings,
        "source_gaps": source_gaps,
        "source_cards": [{"source_ref": f"S{idx:02d}", "summary": "card"} for idx in range(1, 27)],
        "coverage_requirements": [
            {
                "question_id": "q_leaf",
                "requirement_id": f"r{idx}",
                "description": f"Requirement {idx}",
                "rationale": "Needed for the answer.",
                "completion_criterion": "Supported or explicitly deferred.",
                "result": {"status": "unknown", "finding_ids": []},
            }
            for idx in range(1, 5)
        ],
        "questions": ["How does the mechanism work?"],
        "status": "in_progress",
    }
    workspace = {"investigation_jobs": [job], "atom_drafts": [], "research_gaps": []}

    payload = research_job_prompt_payload(workspace, "brief_02", source_map_summary={})
    encoded = json.dumps(payload, ensure_ascii=False)

    assert len(job["source_findings"]) == 127
    assert len(job["source_gaps"]) == 21
    assert len(job["starting_evidence_ids"]) == 98
    assert len(encoded) < 25_000
    assert "handoff_finding_ids" not in encoded
    assert "handoff_evidence_ids" not in encoded
    assert "source_card_previews" not in encoded
    assert "other_job_index" not in encoded
    assert "source_map_summary" not in payload
    assert "source_refs" not in payload["job"]
    assert payload["job"]["source_scope"]["allowed_source_pool"]["source_count"] == 26
    assert payload["job"]["source_scope"]["recommended_source_refs"]
    assert all(
        lead["evidence_ids"]
        for lead in payload["job"]["question_level_leads"]["finding_leads"]
    )


def test_research_job_checkpoint_uses_compact_current_job_not_corpus_inventory() -> None:
    workspace = {
        "current_job": {"job_id": "brief_02"},
        "investigation_jobs": [
            {
                "job_id": "brief_02",
                "coverage_question_ids": ["q_leaf"],
                "questions": ["How does it work?"],
                "source_refs": ["S01"],
                "coverage_requirements": [
                    {
                        "question_id": "q_leaf",
                        "requirement_id": "r1",
                        "description": "Explain the mechanism.",
                        "rationale": "Needed.",
                        "completion_criterion": "Mechanism is evidence-backed.",
                    }
                ],
                "observations": [{"source_refs": ["S01"], "evidence_ids": ["ev1"], "note": "Useful read."}],
                "status": "in_progress",
            }
        ],
        "atom_drafts": [],
        "research_gaps": [],
    }
    initial_payload = research_job_prompt_payload(workspace, "brief_02", source_map_summary={})
    state = {
        "phase": "v2_editorial_research",
        "agent_name": "research_job_agent_brief_02",
        "corpus": {"source_inventory": [{"title": "FULL_CORPUS_MARKER"} for _ in range(100)]},
        "research_workspace": {
            "current_job_detail": initial_payload["job"],
            "corpus_brief_preview": "SHOULD_NOT_APPEAR",
        },
        "durable_paging_progress": {"current_job_source_findings": {"next_cursor": "20"}},
    }

    content = checkpoint_restart_message(
        resume_context=build_checkpoint_resume_context(
            run_config={"user_prompt": "Topic", "language": "ru", "duration_target": 1800},
            workflow_contract=RESEARCH_JOB_RESUME_CONTRACT,
        ),
        reason="input_tokens>=60000",
        previous_chain_id="chain-0",
        next_chain_id="chain-1",
        checkpoint_index=1,
        state=state,
        last_tool_results=[{"tool_name": "read_evidence", "output": {"accepted": True, "evidence_id": "ev1"}}],
    )["content"]

    assert len(content) < len(json.dumps(state, ensure_ascii=False)) + 3_000
    assert "FULL_CORPUS_MARKER" not in content
    assert "SHOULD_NOT_APPEAR" not in content
    assert "current_job_detail" in content
    assert "recent_read_previews" in content


def test_research_job_checkpoint_uses_last_targeted_requirement_from_production_trace() -> None:
    run_id = uuid.uuid4()
    workspace = {
        "current_job": {"job_id": "brief_02"},
        "investigation_jobs": [
            {
                "job_id": "brief_02",
                "coverage_question_ids": ["q_leaf"],
                "questions": ["How does it work?"],
                "source_refs": ["S03"],
                "coverage_requirements": [
                    {"question_id": "q_leaf", "requirement_id": "r1", "description": "Issuers."},
                    {"question_id": "q_leaf", "requirement_id": "r4", "description": "Exception reason."},
                ],
                "status": "in_progress",
            }
        ],
    }
    traces = [
        V2ToolTrace(
            generation_run_id=run_id,
            phase="v2_editorial_research",
            agent_name="research_job_agent_brief_02",
            tool_name="list_research_workspace_items",
            input_json={
                "section": "current_job_source_findings",
                "cursor": "0",
                "limit": 5,
                "requirement_id": "r4",
                "query": "HK$10 residual",
            },
            output_json={
                "accepted": True,
                "section": "current_job_source_findings",
                "filter": {"requirement_id": "r4", "query": "HK$10 residual"},
                "items": [{"finding_id": "f1", "source_ref": "S03", "evidence_ids": ["ev1"]}],
                "page": {"cursor": "0", "next_cursor": "", "returned_count": 1, "total_count": 1},
            },
        )
    ]

    checkpoint = compact_research_job_checkpoint_payload(
        workspace,
        current_job_id="brief_02",
        recent_tool_traces=traces,
    )

    assert checkpoint["current_job_detail"]["last_targeted_requirement_id"] == "r4"
    assert checkpoint["recent_read_previews"][0]["requirement_id"] == "r4"
    assert checkpoint["recent_read_previews"][0]["query"] == "HK$10 residual"
    assert "result_preview" in checkpoint["recent_read_previews"][0]


def test_research_job_checkpoint_does_not_default_active_requirement_to_first() -> None:
    checkpoint = compact_research_job_checkpoint_payload(
        {
            "current_job": {"job_id": "brief_02"},
            "investigation_jobs": [
                {
                    "job_id": "brief_02",
                    "coverage_requirements": [
                        {"question_id": "q", "requirement_id": "r1"},
                        {"question_id": "q", "requirement_id": "r2"},
                    ],
                    "status": "in_progress",
                }
            ],
        },
        current_job_id="brief_02",
    )

    assert checkpoint["current_job_detail"]["last_targeted_requirement_id"] is None


def test_research_job_checkpoint_keeps_actionable_leads_without_writes() -> None:
    workspace = {
        "current_job": {"job_id": "brief_02"},
        "investigation_jobs": [
            {
                "job_id": "brief_02",
                "coverage_question_ids": ["q_leaf"],
                "questions": ["How does it work?"],
                "source_refs": ["S03"],
                "source_findings": [
                    {
                        "finding_id": "f1",
                        "source_ref": "S03",
                        "statement": "The source describes a concrete mechanism.",
                        "limitations": [],
                        "evidence_ids": ["ev1"],
                        "question_ids": ["q_leaf"],
                    }
                ],
                "coverage_requirements": [
                    {"question_id": "q_leaf", "requirement_id": "r1", "description": "Mechanism."}
                ],
                "status": "in_progress",
            }
        ],
        "research_gaps": [],
        "atom_drafts": [],
    }

    checkpoint = compact_research_job_checkpoint_payload(workspace, current_job_id="brief_02")

    assert checkpoint["current_job_detail"]["persisted_progress"]["observations"] == []
    assert checkpoint["current_job_detail"]["question_level_leads"]["finding_leads"][0]["finding_id"] == "f1"
    assert "lineage" not in checkpoint["checkpoint_usage"].lower()


def test_research_job_paging_progress_separates_requirement_and_query_fingerprint() -> None:
    run_id = uuid.uuid4()
    traces = [
        V2ToolTrace(
            generation_run_id=run_id,
            phase="v2_editorial_research",
            agent_name="research_job_agent_brief_02",
            tool_name="list_research_workspace_items",
            input_json={
                "section": "current_job_source_findings",
                "cursor": "0",
                "limit": 5,
                "question_id": "q_leaf",
                "requirement_id": requirement_id,
                "query": f"lookup {requirement_id}",
            },
            output_json={
                "accepted": True,
                "items": [{"finding_id": f"f-{requirement_id}"}],
                "page": {"cursor": "0", "next_cursor": "1", "returned_count": 1, "total_count": 2},
            },
        )
        for requirement_id in ("r1", "r2", "r3", "r4")
    ]

    progress = summarize_paginated_tool_traces(traces)

    assert len(progress) == 4
    assert {item["requirement_id"] for item in progress} == {"r1", "r2", "r3", "r4"}
    assert all(item["question_id"] == "q_leaf" for item in progress)
    assert all(item.get("query_fingerprint") for item in progress)


def test_research_job_persisted_progress_filters_gaps_by_job_not_shared_sources() -> None:
    workspace = {
        "investigation_jobs": [
            {"job_id": "job_a", "source_refs": ["S01"], "status": "in_progress"},
            {"job_id": "job_b", "source_refs": ["S01"], "status": "in_progress"},
        ],
        "research_gaps": [
            {
                "gap_id": "gap_a",
                "job_id": "job_a",
                "job_ids": ["job_a"],
                "requirement_id": "r1",
                "question": "Job A only.",
                "source_refs": ["S01"],
            }
        ],
        "atom_drafts": [],
    }

    payload = research_job_prompt_payload(workspace, "job_b", source_map_summary={})

    assert payload["job"]["persisted_progress"]["research_gaps"] == []


@pytest.mark.asyncio
async def test_research_observation_persists_and_validates_requirement_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    workspace = {
        "current_job": {"job_id": "job_1"},
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "source_refs": ["S01"],
                "coverage_requirements": [{"question_id": "q", "requirement_id": "r1"}],
                "status": "in_progress",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, generation_run_id, workspace):  # noqa: ARG001
        return None

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(source_cards=[_source(title="Source")]),
        mode="job",
        current_job_id="job_1",
    )
    observation_tool = next(tool for tool in tools if tool.name == "record_research_observation")

    accepted = await observation_tool.handler(
        {
            "job_id": "job_1",
            "requirement_id": "r1",
            "source_refs": ["S01"],
            "evidence_ids": [],
            "note": "Useful read advanced the requirement.",
            "pending_questions": [],
        }
    )
    rejected = await observation_tool.handler(
        {
            "job_id": "job_1",
            "requirement_id": "missing",
            "source_refs": ["S01"],
            "evidence_ids": [],
            "note": "Invalid requirement.",
            "pending_questions": [],
        }
    )

    job = workspace["investigation_jobs"][0]
    assert accepted["accepted"] is True
    assert job["observations"][0]["requirement_id"] == "r1"
    assert job["last_targeted_requirement_id"] == "r1"
    assert rejected["accepted"] is False
    assert rejected["error"] == "observation requirement_id must belong to the current research job"


@pytest.mark.asyncio
async def test_research_gap_persists_requirement_scope_without_cross_job_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    workspace = {
        "current_job": {"job_id": "job_a"},
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "job_a",
                "source_refs": ["S01"],
                "coverage_requirements": [{"question_id": "q", "requirement_id": "r1"}],
                "status": "in_progress",
            },
            {
                "job_id": "job_b",
                "source_refs": ["S01"],
                "coverage_requirements": [{"question_id": "q", "requirement_id": "r1"}],
                "status": "in_progress",
            },
        ],
        "research_gaps": [
            {
                "gap_id": "gap_b",
                "job_id": "job_b",
                "job_ids": ["job_b"],
                "question_id": "q",
                "requirement_id": "r1",
                "question": "Same wording.",
                "category": "open_requirement",
                "normalized_key": "open_requirement:job_b:q:r1:same wording",
                "source_refs": ["S01"],
                "evidence_ids": [],
                "atom_refs": [],
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, generation_run_id, workspace):  # noqa: ARG001
        return None

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(source_cards=[_source(title="Source")]),
        mode="job",
        current_job_id="job_a",
    )
    gap_tool = next(tool for tool in tools if tool.name == "record_research_gap")

    accepted = await gap_tool.handler(
        {
            "job_id": "job_a",
            "requirement_id": "r1",
            "question": "Same wording.",
            "category": "open_requirement",
            "severity": "non_blocking",
            "treatment_relevance": "central",
            "source_refs": ["S01"],
            "evidence_ids": [],
            "status": "open",
            "resolution_note": "Still unresolved.",
        }
    )

    assert accepted["accepted"] is True
    assert len(workspace["research_gaps"]) == 2
    job_a_gap = next(item for item in workspace["research_gaps"] if item["job_id"] == "job_a")
    assert job_a_gap["requirement_id"] == "r1"
    assert workspace["investigation_jobs"][0]["last_targeted_requirement_id"] == "r1"


def test_research_job_recommendations_start_with_persisted_recommendations() -> None:
    workspace = {
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "coverage_question_ids": ["q"],
                "questions": ["Question"],
                "recommended_source_refs": ["S09", "S08"],
                "source_refs": ["S01", "S09", "S08"],
                "source_findings": [
                    {
                        "finding_id": "f1",
                        "source_ref": "S01",
                        "statement": "Relevant lead.",
                        "limitations": [],
                        "evidence_ids": ["ev1"],
                        "question_ids": ["q"],
                    }
                ],
                "coverage_requirements": [],
                "status": "in_progress",
            }
        ],
        "research_gaps": [],
        "atom_drafts": [],
    }

    payload = research_job_prompt_payload(workspace, "job_1", source_map_summary={})

    assert payload["job"]["source_scope"]["recommended_source_refs"][:2] == ["S09", "S08"]


def test_question_level_handoff_gaps_are_not_keyword_routed_to_requirements() -> None:
    from app.generation_v2 import research_workspace as workspace_api

    question_gap = {
        "source_ref": "S03",
        "question_id": "q_hkd_current_mechanics",
        "description": "HK$10 residual question mentions HKMA, banks, Exchange Fund and denominations.",
        "evidence_ids": ["ev_gap"],
    }
    explicit_gap = {
        **question_gap,
        "description": "Explicit r4 residual gap.",
        "requirement_id": "q_hkd_mech_r4",
    }

    assert workspace_api._question_handoff_gap_previews(  # noqa: SLF001
        [question_gap],
        question_id="q_hkd_current_mechanics",
    ) == [
        {
            "source_ref": "S03",
            "question_id": "q_hkd_current_mechanics",
            "description": question_gap["description"],
            "evidence_ids": ["ev_gap"],
        }
    ]
    assert (
        workspace_api._explicit_requirement_handoff_gap_previews(  # noqa: SLF001
            [question_gap],
            requirement_id="q_hkd_mech_r1",
            question_id="q_hkd_current_mechanics",
        )
        == []
    )
    assert (
        workspace_api._explicit_requirement_handoff_gap_previews(  # noqa: SLF001
            [explicit_gap],
            requirement_id="q_hkd_mech_r1",
            question_id="q_hkd_current_mechanics",
        )
        == []
    )
    assert workspace_api._explicit_requirement_handoff_gap_previews(  # noqa: SLF001
        [explicit_gap],
        requirement_id="q_hkd_mech_r4",
        question_id="q_hkd_current_mechanics",
    ) == [
        {
            "source_ref": "S03",
            "question_id": "q_hkd_current_mechanics",
            "requirement_id": "q_hkd_mech_r4",
            "description": "Explicit r4 residual gap.",
            "evidence_ids": ["ev_gap"],
        }
    ]


@pytest.mark.asyncio
async def test_requirement_supporting_atoms_are_visible_on_requirement_payload_and_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    workspace = {
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "mapped",
                "questions": ["How does it work?"],
                "question_outcomes": [
                    {
                        "question": "How does it work?",
                        "status": "answered",
                        "note": "Answered.",
                        "evidence_ids": ["ev_1"],
                        "supporting_atom_ids": ["wrong_level"],
                    }
                ],
                "requirement_outcomes": [
                    {
                        "requirement_id": "r1",
                        "status": "answered",
                        "note": "Answered.",
                        "evidence_ids": ["ev_1"],
                        "supporting_atom_ids": ["atom_1"],
                    }
                ],
            }
        ]
    }
    payload = research_job_prompt_payload(workspace, "job_1", source_map_summary={})

    progress = payload["job"]["persisted_progress"]
    assert "supporting_atom_ids" not in progress["question_outcomes"][0]
    assert progress["requirement_outcomes"][0]["supporting_atom_ids"] == ["atom_1"]

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="job",
        current_job_id="job_1",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")
    question_page = await page_tool.handler({"section": "job_question_outcomes", "limit": 1})
    requirement_page = await page_tool.handler({"section": "job_requirement_outcomes", "limit": 1})

    assert "supporting_atom_ids" not in question_page["items"][0]
    assert requirement_page["items"][0]["supporting_atom_ids"] == ["atom_1"]


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

    assert "observations" not in payload["job"]
    assert payload["job"]["persisted_progress"]["observations"][0]["note_preview"].startswith("Observation 0")
    assert payload["job"]["questions"][0]["question"] == "Question 0"
    assert "pages" not in payload["job"]

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
    page = await page_tool.handler({"section": "current_job_questions", "cursor": "6", "limit": 2})

    assert [item["question"] for item in page["items"]] == questions[6:]
    assert all("question_preview" not in item for item in page["items"])
    assert page["page"]["next_cursor"] == ""
    assert page["page"]["omitted_count"] == 0


@pytest.mark.asyncio
async def test_current_job_source_lookup_uses_explicit_links_and_statement_query_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    workspace = {
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "status": "in_progress",
                "coverage_question_ids": ["q"],
                "coverage_requirements": [{"question_id": "q", "requirement_id": "r1"}],
                "source_findings": [
                    {
                        "finding_id": "f-explicit",
                        "source_ref": "S01",
                        "statement": "Low lexical overlap.",
                        "limitations": [],
                        "evidence_ids": ["ev1"],
                        "question_links": [{"question_id": "q", "requirement_ids": ["r1"]}],
                    },
                    {
                        "finding_id": "f-query",
                        "source_ref": "S02",
                        "statement": "The operating mechanism is stated here.",
                        "limitations": [],
                        "evidence_ids": ["ev2"],
                        "question_ids": ["q"],
                    },
                    {
                        "finding_id": "f-summary-only",
                        "source_ref": "S03",
                        "statement": "Irrelevant statement.",
                        "limitations": [],
                        "source_summary": "mechanism appears only in the source summary",
                        "evidence_ids": ["ev3"],
                        "question_ids": ["q"],
                    },
                ],
                "source_gaps": [
                    {
                        "source_ref": "S01",
                        "question_id": "q",
                        "requirement_id": "r1",
                        "description": "Explicit requirement gap.",
                        "evidence_ids": [],
                    },
                    {
                        "source_ref": "S02",
                        "question_id": "q",
                        "description": "Mechanism remains unresolved.",
                        "evidence_ids": [],
                    },
                    {
                        "source_ref": "S03",
                        "question_id": "q",
                        "description": "No useful text.",
                        "source_summary": "mechanism appears only in summary",
                        "evidence_ids": [],
                    },
                ],
            }
        ]
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        mode="job",
        current_job_id="job_1",
    )
    page_tool = next(tool for tool in tools if tool.name == "list_research_workspace_items")

    explicit_page = await page_tool.handler(
        {"section": "current_job_source_findings", "cursor": "0", "limit": 10, "requirement_id": "r1"}
    )
    query_page = await page_tool.handler(
        {"section": "current_job_source_findings", "cursor": "0", "limit": 10, "query": "mechanism"}
    )
    gap_page = await page_tool.handler(
        {"section": "current_job_source_gaps", "cursor": "0", "limit": 10, "query": "mechanism"}
    )

    assert [item["finding_id"] for item in explicit_page["items"]] == ["f-explicit"]
    assert [item["finding_id"] for item in query_page["items"]] == ["f-query"]
    assert "source_summary" not in query_page["items"][0]
    assert [item["source_ref"] for item in gap_page["items"]] == ["S02"]
    assert "source_summary" not in gap_page["items"][0]


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


@pytest.mark.asyncio
async def test_research_planner_upsert_preserves_seeded_candidate_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    workspace = {
        "source_order": ["S01", "S02"],
        "corpus_brief": {
            "coverage_questions": [{"question_id": "q_main", "question": "How does it work?"}],
            "research_handoff": [{"question_ids": ["q_main"]}],
        },
        "investigation_jobs": [
            {
                "job_id": "brief_01",
                "corpus_handoff_id": "handoff:q_main",
                "coverage_question_ids": ["q_main"],
                "corpus_handoff_question_assignments": [
                    {"question_id": "q_main", "requirement_ids": ["req_mechanism"]}
                ],
                "corpus_handoff_finding_ids": ["finding-hinted"],
                "corpus_handoff_final_finding_ids": [],
                "corpus_handoff_candidate_finding_ids": ["finding-hinted"],
                "corpus_handoff_allowed_source_refs": ["S01", "S02"],
                "corpus_handoff_preferred_source_refs": ["S01"],
                "corpus_handoff_candidate_source_refs": ["S01"],
                "corpus_handoff_candidate_evidence_ids": ["evidence-hinted"],
                "corpus_handoff_coverage_hint_ids": ["hint-hinted"],
                "coverage_requirements": [{"question_id": "q_main", "requirement_id": "req_mechanism"}],
                "open_requirements": [{"question_id": "q_main", "requirement_id": "req_mechanism"}],
                "source_findings": [
                    {
                        "finding_id": "finding-hinted",
                        "source_ref": "S01",
                        "statement": "Candidate source finding.",
                        "handoff_link_kind": "candidate",
                    }
                ],
                "label": "Seed",
                "story_job": "mechanism",
                "purpose": "Seeded from handoff.",
                "source_refs": ["S01", "S02"],
                "questions": ["How does it work?"],
                "starting_evidence_ids": ["evidence-hinted"],
                "allow_broad_search": False,
                "status": "planned",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, generation_run_id, workspace: dict):  # noqa: ARG001
        return None

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(source_cards=[_source("source-1"), _source("source-2")]),
        mode="planner",
    )
    upsert = next(tool for tool in tools if tool.name == "upsert_research_job")

    accepted = await upsert.handler(
        {
            "job_id": "brief_01",
            "label": "Mechanism",
            "story_job": "mechanism",
            "purpose": "Resolve the mechanism.",
            "source_refs": [],
            "questions": [],
            "required_atom_kinds": ["mechanism"],
            "priority": "high",
            "allow_broad_search": False,
        }
    )

    assert accepted["accepted"] is True
    job = workspace["investigation_jobs"][0]
    assert job["source_refs"] == ["S01", "S02"]
    assert job["corpus_handoff_candidate_finding_ids"] == ["finding-hinted"]
    assert job["corpus_handoff_allowed_source_refs"] == ["S01", "S02"]
    assert job["corpus_handoff_preferred_source_refs"] == ["S01"]
    assert job["corpus_handoff_candidate_evidence_ids"] == ["evidence-hinted"]
    assert job["corpus_handoff_coverage_hint_ids"] == ["hint-hinted"]
    assert job["coverage_requirements"] == [{"question_id": "q_main", "requirement_id": "req_mechanism"}]
    assert job["source_findings"][0]["handoff_link_kind"] == "candidate"


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
    assert pages["source_map_cards"]["page"]["returned_count"] == 2
    assert pages["source_map_cards"]["page"]["next_cursor"] == ""
    assert pages["source_map_cards"]["items"][0]["evidence_ids"] == evidence_ids
    assert pages["source_map_cards"]["items"][0]["evidence_id_omitted_count"] == 0
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
    atom_index_page = await page_tool.handler({"section": "atom_index", "cursor": "0", "limit": 40})
    atom_page = await page_tool.handler({"section": "atom_drafts", "cursor": "0", "limit": 40})
    observation_page = await page_tool.handler({"section": "observations", "cursor": "1", "limit": 40})

    assert atom_index_page["items"][0]["atom_ref"] == "A01"
    assert "explanation" not in atom_index_page["items"][0]
    assert atom_page["items"][0]["claim"] == claim
    assert atom_page["items"][0]["explanation"] == explanation
    assert atom_page["page"]["returned_count"] == 1
    assert observation_page["items"][0]["note"] == note_two
    assert observation_page["items"][0]["pending_questions"] == ["Second full pending question."]
    assert observation_page["page"]["next_cursor"] == ""
    assert atom_page["items"][0]["evidence_ids"] == ["backend-evidence-1"]
    assert observation_page["items"][0]["evidence_ids"] == ["backend-evidence-2"]
    assert len(json.dumps(atom_page)) < 12000
    assert len(json.dumps(observation_page)) < 12000
    assert model_facing_tool_output("list_research_workspace_items", atom_index_page) is atom_index_page
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
    assert pages["job"]["items"][0]["evidence_ids"] == [hidden_evidence_id]
    assert pages["outcome"]["items"][0]["evidence_ids"] == [hidden_evidence_id]
    assert pages["gap"]["items"][0]["evidence_ids"] == [hidden_evidence_id]
    assert pages["risk"]["items"][0]["evidence_ids"] == [hidden_evidence_id]
    assert pages["feedback"]["items"][0]["evidence_ids"] == [hidden_evidence_id]
    for page in pages.values():
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
        "source_sets": [{"set_id": f"set_{idx}", "status": "mapped", "findings_summary": "x" * 700} for idx in range(3)]
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
    long_finding = "f" * 1600
    long_outcome = "o" * 1200
    workspace = {
        "source_order": ["S01"],
        "coverage_questions": [
            {
                "question_id": "q1",
                "question": "What happened?",
                "answer_requirements": [],
            }
        ],
        "source_sets": [
            {
                "set_id": "set_1",
                "label": "Primary set",
                "purpose": long_purpose,
                "rationale": long_rationale,
                "source_refs": ["S01"],
                "question_ids": ["q1"],
            }
        ],
        "source_assessments": [
            {
                "set_id": "set_1",
                "source_ref": "S01",
                "processing_status": "resolved",
                "representation_status": "readable",
                "disposition": "anchor",
                "finding_ids": ["finding-1"],
                "no_findings_reason": "",
            }
        ],
        "source_findings": [
            {
                "finding_id": "finding-1",
                "set_id": "set_1",
                "source_ref": "S01",
                "statement": long_finding,
                "evidence_ids": ["ev-1"],
                "question_links": [{"question_id": "q1", "requirement_ids": []}],
            }
        ],
        "coverage_outcomes": [
            {
                "set_id": "set_1",
                "question_id": "q1",
                "status": "covered",
                "finding_ids": ["finding-1"],
                "orientation_summary": long_outcome,
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

    assert source_set["purpose"] == long_purpose[:400]
    assert source_set["purpose_omitted_chars"] == 200
    assert source_set["rationale"] == long_rationale[:500]
    assert source_set["rationale_omitted_chars"] == 700
    assert "questions" not in source_set
    assert "observations" not in source_set
    assert "written_actions" not in source_set
    assert source_set["source_assessment_count"] == 1
    assert source_set["source_finding_count"] == 1
    assert "legacy_coverage_outcome_count" not in source_set
    assert payload["saved_state_previews"]["findings"][0]["statement"] == long_finding[:320]
    assert payload["saved_state_previews"]["findings"][0]["statement_omitted_chars"] == 1280
    assert "legacy_question_outcomes" not in payload["saved_state_previews"]
    assert "set_overview" in payload["saved_state_previews"]["full_records"]
    assert payload["capsule_previews"][0]["summary_hint_omitted_chars"] == 60
    assert payload["capsule_previews"][0]["windows"][0]["text_omitted_chars"] == 100
    assert workspace["source_sets"][0]["purpose"] == long_purpose
    assert workspace["source_findings"][0]["statement"] == long_finding
    assert workspace["coverage_outcomes"][0]["orientation_summary"] == long_outcome


@pytest.mark.asyncio
async def test_triage_set_agent_can_restore_full_durable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    purpose = "p" * 600
    rationale = "r" * 1200
    finding_statement = "f" * 1600
    outcome_summary = "o" * 1600
    evidence_ids = [f"ev-{index}" for index in range(12)]
    workspace = {
        "source_order": ["S01"],
        "current_set": {"set_id": "set_1"},
        "coverage_questions": [
            {
                "question_id": "q1",
                "question": "q" * 280,
                "kind": "cause",
                "importance": "critical",
                "answer_requirements": [],
            }
        ],
        "source_sets": [
            {
                "set_id": "set_1",
                "label": "Primary set",
                "purpose": purpose,
                "source_refs": ["S01"],
                "rationale": rationale,
                "question_ids": ["q1"],
                "priority": "high",
                "status": "in_progress",
            }
        ],
        "source_assessments": [
            {
                "assessment_id": "assessment:source-1",
                "set_id": "set_1",
                "source_ref": "S01",
                "source_id": "source-1",
                "processing_status": "resolved",
                "representation_status": "readable",
                "disposition": "anchor",
                "finding_ids": ["finding-1"],
                "no_findings_reason": "",
                "evidence_ids": evidence_ids,
            }
        ],
        "source_findings": [
            {
                "finding_id": "finding-1",
                "set_id": "set_1",
                "source_ref": "S01",
                "source_id": "source-1",
                "statement": finding_statement,
                "evidence_ids": evidence_ids,
                "scope": {
                    "actor": "Actor",
                    "period": "Period",
                    "geography": "Place",
                    "qualifiers": [],
                },
                "limitations": [],
                "question_links": [{"question_id": "q1", "requirement_ids": []}],
                "source_relations": [],
                "semantic_revision": 1,
            }
        ],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome:q1:set_1",
                "set_id": "set_1",
                "question_id": "q1",
                "status": "covered",
                "source_refs": ["S01"],
                "source_ids": ["source-1"],
                "evidence_ids": evidence_ids,
                "finding_ids": ["finding-1"],
                "requirement_results": [],
                "orientation_summary": outcome_summary,
                "missing_detail": "",
                "conflict_summary": "",
                "deferred_reason": "",
                "is_stale": False,
            }
        ],
    }
    workspace["coverage_outcomes"][0]["workbench_snapshot_id"] = question_workbench_signature(
        workspace,
        workspace["source_sets"][0],
        question_id="q1",
    )
    compact = source_set_prompt_payload(workspace, "set_1")
    assert compact["set"]["purpose"] == purpose[:400]
    assert compact["set"]["purpose_omitted_chars"] == 200
    assert compact["saved_state_previews"]["findings"][0]["statement"] == finding_statement[:320]
    assert compact["saved_state_previews"]["findings"][0]["statement_omitted_chars"] == 1280
    assert "legacy_question_outcomes" not in compact["saved_state_previews"]

    async def load(_db, _run_id):
        return workspace

    async def save(_db, *, workspace, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    set_tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_1",
    )
    page_tool = next(tool for tool in set_tools if tool.name == "read_triage_set_state")
    state_page = await page_tool.handler(
        {"section": "set_overview", "set_id": "set_1", "cursor": "0", "limit": 25}
    )
    question_page = await page_tool.handler(
        {"section": "question_index", "cursor": "0", "limit": 25}
    )
    assessment_page = await page_tool.handler(
        {"section": "source_memory", "source_ref": "S01", "cursor": "0", "limit": 25}
    )

    assert state_page["state"] == {
        "pending_source_refs": [],
        "pending_exact_read_source_refs": [],
        "pending_comparison_source_refs": [],
        "stale_source_analysis_refs": [],
        "assigned_question_ids": ["q1"],
        "cross_question_ids": [],
        "discovered_question_ids": [],
        "saved_source_analysis_count": 1,
        "source_read_receipt_count": 0,
        "latest_source_read_receipts": [],
        "saved_reading_note_count": 0,
        "saved_finding_count": 1,
        "saved_coverage_hint_count": 0,
    }
    assert {item["item_kind"] for item in state_page["items"]} == {
        "source",
        "question",
    }
    question_json = json.dumps(question_page, ensure_ascii=False)
    source_json = json.dumps(assessment_page, ensure_ascii=False)
    assert "q" * 280 in question_json
    assert outcome_summary not in question_json
    assert finding_statement in source_json
    assert all(evidence_id in source_json for evidence_id in evidence_ids)
    model_facing_tool_output("read_triage_set_state", state_page)
    model_facing_tool_output("read_triage_set_state", question_page)
    model_facing_tool_output("read_triage_set_state", assessment_page)

    finalizer_tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="finalizer",
    )
    finalizer_page_tool = next(tool for tool in finalizer_tools if tool.name == "list_triage_source_sets")
    finalizer_finding_page = await finalizer_page_tool.handler(
        {"section": "source_findings", "cursor": "0", "limit": 25}
    )
    assert finalizer_finding_page["items"][0]["statement"] == finding_statement
    assert finalizer_finding_page["items"][0]["evidence_ids"] == evidence_ids


@pytest.mark.asyncio
async def test_triage_set_resume_pages_restore_assigned_questions_and_only_own_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    workspace = {
        "source_order": ["S01", "S02"],
        "current_set": {"set_id": "set_2"},
        "coverage_questions": [
            {"question_id": "q1", "question": "First full question", "kind": "context"},
            {"question_id": "q2", "question": "Second full question", "kind": "cause"},
        ],
        "source_sets": [
            {"set_id": "set_1", "source_refs": ["S01"], "question_ids": ["q1"], "status": "mapped"},
            {"set_id": "set_2", "source_refs": ["S02"], "question_ids": ["q2"], "status": "in_progress"},
        ],
        "source_assessments": [
            {"set_id": "set_1", "source_ref": "S01", "processing_status": "resolved"},
            {"set_id": "set_2", "source_ref": "S02", "processing_status": "resolved"},
        ],
        "coverage_outcomes": [
            {"set_id": "set_1", "question_id": "q1", "status": "missing"},
            {"set_id": "set_2", "question_id": "q2", "status": "partial"},
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_2",
    )
    pager = next(tool for tool in tools if tool.name == "read_triage_set_state")

    current = await pager.handler({"section": "set_overview", "cursor": "0", "limit": 25})
    question_index = await pager.handler(
        {"section": "question_index", "cursor": "0", "limit": 25}
    )
    assessments = await pager.handler(
        {"section": "source_memory", "source_ref": "S02", "cursor": "0", "limit": 25}
    )
    foreign_set = await pager.handler(
        {"section": "source_memory", "source_ref": "S01", "set_id": "set_1", "cursor": "0", "limit": 1}
    )

    assert current["set_id"] == "set_2"
    assert "Second full question" in json.dumps(question_index, ensure_ascii=False)
    assert "set_2" in json.dumps(assessments, ensure_ascii=False)
    assert "set_1" not in json.dumps(assessments, ensure_ascii=False)
    assert foreign_set["accepted"] is False
    assert foreign_set["error"] == "triage_source_set_scope_mismatch"


@pytest.mark.asyncio
async def test_targeted_repair_pages_old_records_until_replacements_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    source_id = str(uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "current_set": {"set_id": "set_1"},
        "source_sets": [
            {
                "set_id": "set_1",
                "source_refs": ["S01"],
                "question_ids": ["q1"],
                "status": "in_progress",
                "repair_targeted": True,
            }
        ],
        "source_assessments": [],
        "coverage_outcomes": [],
        "repair_baseline_source_assessments": [
            {
                "set_id": "set_1",
                "original_set_id": "wrong_set",
                "source_ref": "S01",
                "source_id": source_id,
                "processing_status": "resolved",
                "representation_reason": "Old useful assessment detail.",
                "repair_baseline": True,
                "repair_baseline_version": 1,
            },
            {
                "set_id": "set_1",
                "source_ref": "S01",
                "source_id": source_id,
                "processing_status": "resolved",
                "representation_reason": "Second old assessment detail.",
                "repair_baseline": True,
                "repair_baseline_version": 2,
            },
        ],
        "repair_baseline_coverage_outcomes": [
            {
                "set_id": "set_1",
                "question_id": "q1",
                "status": "partial",
                "source_ids": [source_id],
                "orientation_summary": "Old useful outcome detail.",
                "repair_baseline": True,
                "repair_baseline_version": 1,
            },
            {
                "set_id": "set_1",
                "question_id": "q1",
                "status": "partial",
                "source_ids": [source_id],
                "orientation_summary": "Second old outcome detail.",
                "repair_baseline": True,
                "repair_baseline_version": 2,
            },
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_1",
    )
    pager = next(tool for tool in tools if tool.name == "read_triage_set_state")

    old_assessment = await pager.handler(
        {"section": "source_assessments", "cursor": "0", "limit": 3}
    )
    old_outcome = await pager.handler(
        {"section": "coverage_outcomes", "cursor": "0", "limit": 3}
    )

    assert old_assessment["accepted"] is True
    assert "Old useful assessment detail." in json.dumps(old_assessment, ensure_ascii=False)
    assert "Second old assessment detail." in json.dumps(old_assessment, ensure_ascii=False)
    assert old_outcome["accepted"] is True
    assert "Old useful outcome detail." in json.dumps(old_outcome, ensure_ascii=False)
    assert "Second old outcome detail." in json.dumps(old_outcome, ensure_ascii=False)

    workspace["source_assessments"] = [
        {
            "set_id": "set_1",
            "source_ref": "S01",
            "source_id": source_id,
            "processing_status": "needs_repair",
            "representation_reason": "Unfinished replacement.",
        }
    ]
    workspace["coverage_outcomes"] = [
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "needs_repair",
            "orientation_summary": "Unfinished replacement.",
        }
    ]
    unfinished_memory = await pager.handler(
        {"section": "source_memory", "source_ref": "S01", "cursor": "0", "limit": 3}
    )

    assert "Unfinished replacement." in json.dumps(unfinished_memory, ensure_ascii=False)

    workspace["source_assessments"] = [
        {
            "set_id": "set_1",
            "source_ref": "S01",
            "source_id": source_id,
            "processing_status": "resolved",
            "representation_reason": "Replacement assessment.",
        }
    ]
    workspace["coverage_outcomes"] = [
        {
            "set_id": "set_1",
            "question_id": "q1",
            "status": "partial",
            "source_refs": ["S01"],
            "orientation_summary": "Replacement outcome.",
        }
    ]

    new_memory = await pager.handler(
        {"section": "source_memory", "source_ref": "S01", "cursor": "0", "limit": 1}
    )

    new_json = json.dumps(new_memory, ensure_ascii=False)
    assert "Replacement assessment." in new_json
    assert "Replacement outcome." not in new_json
    assert "repair_baseline" not in new_json


@pytest.mark.asyncio
async def test_large_legacy_question_list_is_read_only_through_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    questions = [f"Legacy question {index}: " + "q" * 500 for index in range(30)]
    workspace = {
        "source_order": ["S01"],
        "current_set": {"set_id": "set_1"},
        "source_sets": [
            {
                "set_id": "set_1",
                "source_refs": ["S01"],
                "questions": questions,
                "status": "in_progress",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_1",
    )
    pager = next(tool for tool in tools if tool.name == "read_triage_set_state")

    current = await pager.handler({"section": "set_overview", "cursor": "0", "limit": 1})
    first_page = await pager.handler(
        {"section": "assigned_questions", "cursor": "0", "limit": 3}
    )

    assert current["accepted"] is True
    assert "questions" not in json.dumps(current, ensure_ascii=False)
    assert first_page["accepted"] is False
    assert first_page["error"] == "triage_workspace_section_not_available_to_agent"


@pytest.mark.asyncio
async def test_many_assigned_questions_never_make_current_set_page_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    questions = [
        {
            "question_id": f"q{index}",
            "question": "q" * 280,
            "kind": "cause",
            "importance": "critical",
            "rationale": "r" * 800,
        }
        for index in range(10)
    ]
    workspace = {
        "source_order": ["S01"],
        "current_set": {"set_id": "set_1"},
        "coverage_questions": questions,
        "source_sets": [
            {
                "set_id": "set_1",
                "label": "Set",
                "purpose": "p" * 600,
                "rationale": "r" * 1200,
                "source_refs": ["S01"],
                "question_ids": [item["question_id"] for item in questions],
                "status": "in_progress",
            }
        ],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="set",
        current_set_id="set_1",
    )
    pager = next(tool for tool in tools if tool.name == "read_triage_set_state")

    current = await pager.handler({"section": "set_overview", "cursor": "0", "limit": 1})
    first_questions = await pager.handler(
        {"section": "question_index", "cursor": "0", "limit": 3}
    )

    assert current["accepted"] is True
    assert first_questions["accepted"] is True
    assert first_questions["page"]["total_count"] == 10
    assert first_questions["page"]["returned_count"] == 3
    assert first_questions["page"]["next_cursor"] == "3"


@pytest.mark.asyncio
async def test_triage_pages_restore_full_plan_summary_and_deferred_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_api

    plan_summary = "p" * 1600
    deferred_reason = "d" * 1000
    workspace = {
        "plan_summary": plan_summary,
        "deferred_sources": [{"source_ref": f"S{index:02d}", "reason": deferred_reason} for index in range(1, 26)],
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
    plan_page = await page_tool.handler({"section": "plan_summary", "cursor": "0", "limit": 1})
    deferred_page = await page_tool.handler({"section": "deferred_sources", "cursor": "24", "limit": 1})

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
async def test_audit_can_read_evidence_grounding_answered_job_outcome() -> None:
    source = _source()
    atom_evidence_id = str(uuid.uuid4())
    outcome_evidence_id = str(uuid.uuid4())
    requirement_evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Supported answer.",
        research_synthesis="Digest.",
        editorial_atoms=[
            EditorialAtom(
                kind="context",
                title="Context",
                claim="Established context.",
                explanation="Established context.",
                source_ids=[source.source_id],
                evidence_ids=[atom_evidence_id],
                story_value="Sets the baseline.",
            )
        ],
    )
    workspace = {
        "investigation_jobs": [
            {
                "job_id": "job-mechanism",
                "questions": ["What mechanism caused the change?"],
                "question_outcomes": [
                    {
                        "question": "What mechanism caused the change?",
                        "status": "answered",
                        "note": "The second evidence item answers it.",
                        "evidence_ids": [outcome_evidence_id],
                    }
                ],
                "requirement_outcomes": [
                    {
                        "requirement_id": "req_mechanism",
                        "status": "answered",
                        "note": "The third evidence item answers the requirement.",
                        "evidence_ids": [requirement_evidence_id],
                    }
                ],
                "status": "mapped",
            }
        ]
    }
    records = [
        {
            "id": atom_evidence_id,
            "source_id": source.source_id,
            "locator_text": "section 1",
            "snippet_text": "Baseline evidence.",
        },
        {
            "id": outcome_evidence_id,
            "source_id": source.source_id,
            "locator_text": "section 2",
            "snippet_text": "Mechanism evidence.",
        },
        {
            "id": requirement_evidence_id,
            "source_id": source.source_id,
            "locator_text": "section 3",
            "snippet_text": "Requirement evidence.",
        },
    ]
    catalog, evidence_id_by_ref, required_refs = audit_evidence_catalog(
        research_memo=memo,
        evidence_records=records,
        context=build_source_ref_context(SourceMap(source_cards=[source])),
        source_titles_by_id={source.source_id: source.title},
        workspace=workspace,
    )
    evidence_ref_by_id = {evidence_id: evidence_ref for evidence_ref, evidence_id in evidence_id_by_ref.items()}
    outcome_ref = evidence_ref_by_id[outcome_evidence_id]
    requirement_ref = evidence_ref_by_id[requirement_evidence_id]

    page_tool = audit_workspace_page_tool(
        research_memo=memo,
        source_map=SourceMap(),
        source_inventory=SourceMap(source_cards=[source]),
        workspace=workspace,
        evidence_previews_by_atom_ref=catalog,
        evidence_ref_by_id=evidence_ref_by_id,
        required_evidence_refs=required_refs,
    )
    outcome_page = await page_tool.handler(
        {
            "section": "job_question_outcomes",
            "cursor": "0",
            "limit": 1,
            "job_id": "job-mechanism",
        }
    )
    requirement_page = await page_tool.handler(
        {
            "section": "job_requirement_outcomes",
            "cursor": "0",
            "limit": 1,
            "job_id": "job-mechanism",
        }
    )

    read_arguments: dict[str, str] = {}

    async def read_handler(arguments: dict[str, str]) -> dict[str, object]:
        read_arguments.update(arguments)
        return {
            "found": True,
            "evidence": {
                "evidence_id": arguments["evidence_id"],
                "source_id": source.source_id,
                "snippet_text": "Mechanism evidence.",
            },
        }

    read_refs: set[str] = set()
    wrapped_tools = audit_tools_with_evidence_refs(
        [
            AgentTool(
                name="read_evidence",
                description="Read evidence.",
                parameters={"type": "object"},
                handler=read_handler,
            )
        ],
        evidence_id_by_ref=evidence_id_by_ref,
        read_refs=read_refs,
    )
    result = await wrapped_tools[0].handler({"evidence_ref": outcome_ref})

    assert outcome_page["items"][0]["evidence_refs"] == [outcome_ref]
    assert outcome_page["items"][0]["required_evidence_refs"] == [outcome_ref]
    assert outcome_page["items"][0]["unavailable_evidence_count"] == 0
    assert outcome_evidence_id not in str(outcome_page)
    assert outcome_ref in required_refs
    assert requirement_page["items"][0]["evidence_refs"] == [requirement_ref]
    assert requirement_page["items"][0]["required_evidence_refs"] == [requirement_ref]
    assert requirement_evidence_id not in str(requirement_page)
    assert requirement_ref in required_refs
    assert read_arguments == {"evidence_id": outcome_evidence_id}
    assert read_refs == {outcome_ref}
    assert outcome_evidence_id not in json.dumps(result)


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
        "corpus_brief": {
            "summary": "Triage handoff summary.",
            "coverage_questions": [
                {
                    "question_id": "q-main",
                    "kind": "mechanism",
                    "importance": "critical",
                    "question": "What mechanism does the corpus establish?",
                    "rationale": "This is the central handoff question.",
                    "parent_question_id": "",
                }
            ],
            "coverage_outcomes": [
                {
                    "question_id": "q-main",
                    "status": "covered",
                    "source_ids": [source.source_id],
                    "evidence_ids": ["backend-evidence-id"],
                    "orientation_summary": "The source establishes a bounded mechanism.",
                    "missing_detail": "",
                    "conflict_summary": "",
                    "deferred_reason": "",
                    "next_action": "research",
                }
            ],
            "research_handoff": [
                {
                    "handoff_id": "handoff-main",
                    "question_ids": ["q-main"],
                    "action": "research",
                    "priority": "high",
                    "preferred_source_ids": [source.source_id],
                    "starting_evidence_ids": ["backend-evidence-id"],
                    "objective": "Verify and explain the mechanism.",
                    "reason": "Triage found a supported starting point.",
                }
            ],
            "gap_records": [
                {
                    "gap_id": "gap:earlier",
                    "kind": "coverage",
                    "description": "An earlier gap.",
                    "source_ids": [source.source_id],
                    "evidence_ids": [],
                },
                {
                    "gap_id": "gap:stable",
                    "kind": "contradiction",
                    "description": "A contradiction remains bounded.",
                    "source_ids": [source.source_id],
                    "evidence_ids": ["backend-evidence-id"],
                },
            ],
        },
        "investigation_jobs": [
            {
                "job_id": "job-main",
                "corpus_handoff_id": "handoff-main",
                "coverage_question_ids": ["q-main"],
                "label": "Mechanism",
                "purpose": "Answer the handoff question.",
                "questions": ["What mechanism does the corpus establish?"],
                "question_outcomes": [
                    {
                        "question": "What mechanism does the corpus establish?",
                        "status": "answered",
                        "note": "The evidence supports the bounded mechanism.",
                        "evidence_ids": ["backend-evidence-id"],
                    }
                ],
                "status": "mapped",
                "summary": "The job answered its assigned question.",
            }
        ],
        "processed_jobs": [
            {"job_id": f"job-{index}", "status": "finished", "summary": long_summary} for index in range(25)
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
            {"risk": "workspace risk " * 100, "evidence_ids": ["backend-evidence-id"]} for _ in range(25)
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
    assert payload["corpus_brief"]["research_handoff"][0]["handoff_id"] == "handoff-main"
    assert payload["corpus_brief"]["coverage_outcomes"][0]["source_refs"] == ["S01"]
    assert payload["corpus_brief"]["gap_records"][1]["gap_ref"] == public_gap_ref(
        "gap:stable"
    )
    assert payload["workspace"]["investigation_jobs"][0]["coverage_question_ids"] == ["q-main"]
    assert source.source_id not in json.dumps(payload)
    assert "backend-evidence-id" not in json.dumps(payload)

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
    handoff_page = await tool.handler(
        {"section": "corpus_brief_handoff", "cursor": "0", "limit": 1}
    )
    question_page = await tool.handler(
        {"section": "job_questions", "cursor": "0", "limit": 1, "job_id": "job-main"}
    )
    outcome_page = await tool.handler(
        {
            "section": "job_question_outcomes",
            "cursor": "0",
            "limit": 1,
            "job_id": "job-main",
        }
    )
    brief_gap_page = await tool.handler(
        {"section": "corpus_brief_gap_records", "cursor": "1", "limit": 1}
    )

    assert atom_page["items"][0]["claim"] == long_claim
    assert atom_page["page"]["returned_count"] == 1
    assert coverage_page["items"][-1]["text"] == memo.source_coverage[-1]
    assert coverage_page["page"]["next_cursor"] == ""
    assert gap_page["items"][0]["question"] == workspace["research_gaps"][0]["question"]
    assert "backend-evidence-id" not in str(atom_page)
    assert "backend-evidence-id" not in str(gap_page)
    assert handoff_page["items"][0]["preferred_source_refs"] == ["S01"]
    assert question_page["items"][0]["question"] == "What mechanism does the corpus establish?"
    assert outcome_page["items"][0]["status"] == "answered"
    assert brief_gap_page["items"][0]["gap_ref"] == public_gap_ref("gap:stable")
    assert outcome_page["items"][0]["evidence_refs"] == []
    assert outcome_page["items"][0]["unavailable_evidence_count"] == 1
    assert "backend-evidence-id" not in str(handoff_page)
    assert "backend-evidence-id" not in str(outcome_page)


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
async def test_exact_quote_match_returns_canonical_selected_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    version = SimpleNamespace(
        id=version_id,
        source_id=source_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="current.txt",
    )
    canonical_text = "Heading\n\nThe exact quoted statement.\n\nTail."

    class Result:
        def scalars(self):
            return [version]

    class Db:
        async def execute(self, _query):
            return Result()

    toolset = SimpleNamespace(
        db=Db(),
        run_view=SimpleNamespace(selected_source_version_ids=[str(version_id)]),
    )
    monkeypatch.setattr(
        "app.generation_v2.tools.evidence_model_read_persistence.get_minio_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "app.generation_v2.tools.evidence_model_read_persistence.download_text",
        lambda _client, _path: canonical_text,
    )

    match = await EvidenceModelReadPersistenceMixin._canonical_quote_match(
        toolset,  # type: ignore[arg-type]
        version,  # type: ignore[arg-type]
        "The exact quoted statement.",
    )

    assert match is not None
    matched_version, text, start, end = match
    assert matched_version is version
    assert text[start:end] == "The exact quoted statement."


@pytest.mark.asyncio
async def test_verified_exact_quote_persists_the_canonical_source_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    canonical_text = "Heading\n\nThe Exact Quoted Statement.\n\nTail."
    start = canonical_text.index("The Exact")
    end = start + len("The Exact Quoted Statement.")
    version = SimpleNamespace(
        id=version_id,
        source_id=source_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
    )
    provider_file = SimpleNamespace(
        id=uuid.uuid4(),
        provider_file_id="file-1",
        provider_vector_store_file_id="vs-file-1",
        attributes={},
    )
    captured: dict = {}

    async def canonical_match(_version, _quote):
        return version, canonical_text, start, end

    async def persist(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "app.generation_v2.tools.evidence_model_read_persistence.persist_evidence_item",
        persist,
    )
    toolset = SimpleNamespace(
        db=object(),
        run=SimpleNamespace(id=run_id, project_id=project_id),
        provider_index=SimpleNamespace(provider_vector_store_id="vs-1"),
        _canonical_quote_match=canonical_match,
    )
    read_result = {
        "quote_text": "the exact quoted statement.",
        "snippet_text": "the exact quoted statement.",
        "support_summary": "Direct support.",
        "uncertainty": "",
        "support_level": "direct",
        "confidence": 0.9,
        "locator_text": "",
        "locator_type": "unknown",
        "_model_read_file_id": "model-file-1",
    }

    await EvidenceModelReadPersistenceMixin._persist_model_read_evidence(
        toolset,  # type: ignore[arg-type]
        provider_file=provider_file,  # type: ignore[arg-type]
        version=version,  # type: ignore[arg-type]
        read_result=read_result,
        response_id="response-1",
        focus_question="What is stated?",
        claim_text="",
        purpose="research",
        locator_hint="",
        cache_key="cache-1",
        request_cache_key="request-1",
    )

    assert read_result["quote_text"] == "The Exact Quoted Statement."
    assert read_result["model_reported_quote_text"] == "the exact quoted statement."
    assert captured["normalized_quote_text"] == "the exact quoted statement."
    assert captured["metadata"]["quote_text"] == "The Exact Quoted Statement."
    assert captured["metadata"]["exact_quote_verified"] is True


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


@pytest.mark.asyncio
async def test_audit_repair_drops_unknown_legacy_scope_and_falls_back_to_broad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "selected_evidence_ids": ["current-evidence"],
        "research_gaps": [],
        "investigation_jobs": [],
    }
    saved: list[dict] = []

    async def load(_db, _run_id):
        return workspace

    async def save(_db, **kwargs):
        saved.append(deepcopy(kwargs["workspace"]))
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", save)
    audit = ResearchAudit(
        passed=False,
        score=0.3,
        verdict="repair",
        issues=[
            ResearchAuditIssue(
                severity="blocking",
                target_step="editorial_research",
                description="Resolve the missing mechanism support.",
                source_refs=["S99"],
                evidence_refs=["E001"],
                evidence_ids=["invented-evidence"],
            )
        ],
    )

    repair_job_ids = await enqueue_audit_research_repair_jobs(  # type: ignore[arg-type]
        None,
        run=run,
        audit=audit,
    )
    gap = saved[0]["research_gaps"][0]
    repair_job = saved[-1]["investigation_jobs"][-1]

    assert len(repair_job_ids) == 1
    assert gap["source_refs"] == []
    assert gap["evidence_ids"] == []
    assert repair_job["source_refs"] == []
    assert repair_job["allow_broad_search"] is True
    assert "re-establish source scope" in gap["resolution_note"]
    assert "re-establish grounding" in gap["resolution_note"]
    assert saved[0]["audit_repair_feedback"][0]["source_refs"] == []
    assert saved[0]["audit_repair_feedback"][0]["evidence_ids"] == []


@pytest.mark.asyncio
async def test_exhausted_audit_repair_does_not_mutate_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as workspace_api

    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "research_gaps": [],
        "memo_fields": {"title": "Keep the durable memo"},
        "repair_budget": {TARGETED_REPAIR_BUDGET: 1},
    }
    original = deepcopy(workspace)

    async def load(_db, _run_id):
        return workspace

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("an exhausted repair must not save or enqueue work")

    monkeypatch.setattr(workspace_api, "load_research_workspace", load)
    monkeypatch.setattr(workspace_api, "save_research_workspace", unexpected)
    monkeypatch.setattr(workspace_api, "enqueue_research_repair_jobs", unexpected)
    audit = ResearchAudit(
        passed=False,
        score=0.3,
        verdict="repair",
        issues=[
            ResearchAuditIssue(
                severity="blocking",
                target_step="editorial_research",
                description="Resolve the missing mechanism support.",
            )
        ],
    )

    assert await enqueue_audit_research_repair_jobs(None, run=run, audit=audit) == []  # type: ignore[arg-type]
    assert workspace == original


@pytest.mark.asyncio
async def test_pending_treatment_repair_resumes_without_mutating_workspace() -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "research_gaps": [],
        "memo_fields": {"title": "Keep the durable memo"},
        "repair_operations": {
            TARGETED_REPAIR_BUDGET: {
                "kind": "research_jobs",
                "status": REPAIR_IN_PROGRESS,
                "payload": {"repair_job_ids": ["repair-job-1"]},
            }
        },
    }
    original = deepcopy(workspace)

    async def load(_db, _run_id):
        return workspace

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("a pending repair must resume its durable operation")

    gate = EpisodeCritique(
        passed=False,
        summary="failed",
        weak_sections=["Treatment planning gap requires research repair: ground the mechanism."],
        rewrite_instructions=[],
    )

    repair_jobs = await enqueue_treatment_research_repair_jobs(
        None,  # type: ignore[arg-type]
        run=run,
        treatment_gate=gate,
        load_workspace=load,
        save_workspace=unexpected,
        enqueue_repair_jobs=unexpected,
    )

    assert repair_jobs == ["repair-job-1"]
    assert workspace == original
