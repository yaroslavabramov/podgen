from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2 import source_triage_read_checkpointing as tracking
from app.generation_v2.pipeline.source_triage_set_flow import _set_progress_state
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_source_analysis_contract import (
    validate_reading_basis,
)
from app.generation_v2.source_triage_grounding_audit_state import (
    current_grounding_audit_violations,
)
from app.generation_v2.source_triage_workspace_tool_handlers import (
    SourceTriageWorkspaceToolHandlers,
)
from app.generation_v2.source_triage_workspace_tool_specs import coverage_outcome_tool
from app.generation_v2.source_triage_workspace_tool_specs import source_analysis_tool


class _EvidenceToolset:
    def __init__(self, evidence: dict[str, SimpleNamespace]) -> None:
        self.evidence = evidence

    async def _evidence_in_run(self, evidence_id: str):
        return self.evidence.get(evidence_id)

    async def _source_ref_for_id(self, _source_id) -> str:
        return "S01"

    async def _select_evidence_ids_for_write(self, _evidence_ids: list[str], **_kwargs):
        return []

    @staticmethod
    def _evidence_has_current_exact_read(_evidence: SimpleNamespace) -> bool:
        return True


def _strict_audit_args(*, finding_id: str, verdict: str = "supported") -> dict:
    return {
        "finding_id": finding_id,
        "verdict": verdict,
        "rationale": "The exact passage supports the whole bounded statement.",
        "revised_statement": "",
        "revised_support_explanation": "",
        "revised_evidence_ids": [],
        "revised_scope": {"actor": "", "period": "", "geography": "", "qualifiers": []},
        "revised_limitations": [],
        "source_no_findings_reason": "",
    }


@pytest.mark.asyncio
async def test_agent_cannot_switch_sources_before_semantic_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01", "S02"]}],
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:S01:1",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "sequential",
                "start_offset": 0,
                "end_offset": 4_000,
                "total_chars": 10_000,
                "delivery_status": "delivered",
                "delivered_at": "2026-07-20T10:00:00",
                "request": {"cursor": "", "max_chars": 4_000},
            }
        ],
        "reading_notes": [],
        "source_assessments": [],
    }

    async def load(_db, _run_id):
        return workspace

    monkeypatch.setattr(tracking, "load_source_triage_workspace", load)
    blocked = await tracking.source_checkpoint_transition_guard(
        None,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        current_set_id="set-1",
        target_source_refs=["S02"],
        exact_read=False,
    )

    assert blocked is not None
    assert blocked["error"] == "checkpoint_current_source_before_switch"
    assert blocked["checkpoint_required_source_refs"] == ["S01"]

    workspace["reading_notes"] = [
        {
            "set_id": "set-1",
            "source_ref": "S01",
            "receipt_refs": ["receipt:S01:1"],
            "what_was_read": "The delivered opening section.",
            "what_was_established": "Its bounded argument and remaining uncertainty.",
        }
    ]
    allowed = await tracking.source_checkpoint_transition_guard(
        None,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        current_set_id="set-1",
        target_source_refs=["S02"],
        exact_read=False,
    )
    assert allowed is None

    comparison_blocked = await tracking.source_checkpoint_transition_guard(
        None,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        current_set_id="set-1",
        target_source_refs=["S01", "S02"],
        exact_read=False,
    )
    assert comparison_blocked is not None
    assert comparison_blocked["error"] == "checkpoint_sources_before_comparison"
    assert comparison_blocked["checkpoint_required_source_refs"] == ["S02"]

    workspace["source_assessments"] = [
        {
            "set_id": "set-1",
            "source_ref": "S02",
            "processing_status": "resolved",
            "finding_ids": ["finding:S02:1"],
            "updated_at": "2026-07-20T10:01:00",
        }
    ]
    comparison_allowed = await tracking.source_checkpoint_transition_guard(
        None,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        current_set_id="set-1",
        target_source_refs=["S01", "S02"],
        exact_read=False,
    )
    assert comparison_allowed is None

    workspace["source_read_receipts"].append(
        {
            "receipt_ref": "receipt:S01:2",
            "set_id": "set-1",
            "source_ref": "S01",
            "source_version_id": "version-1",
            "read_kind": "sequential",
            "start_offset": 4_000,
            "end_offset": 8_000,
            "total_chars": 10_000,
            "delivery_status": "delivered",
            "delivered_at": "2026-07-20T10:02:00",
            "request": {"cursor": "4_000", "max_chars": 4_000},
        }
    )
    comparison_after_new_read = await tracking.source_checkpoint_transition_guard(
        None,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        current_set_id="set-1",
        target_source_refs=["S01", "S02"],
        exact_read=False,
    )
    assert comparison_after_new_read is not None
    assert comparison_after_new_read["checkpoint_required_source_refs"] == ["S01"]

    async def save(_db, *, workspace: dict, **_kwargs):
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(tracking, "save_source_triage_workspace", save)
    comparison_control = await tracking.record_triage_comparison_read(
        None,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        current_set_id="set-1",
        evidence_ids_by_source_ref={"S01": ["evidence-1"], "S02": ["evidence-2"]},
    )
    assert comparison_control is not None
    assert comparison_control["pending_source_refs"] == ["S01", "S02"]
    tracking.clear_pending_comparison_read(workspace["source_sets"][0], source_ref="S01")
    assert tracking.pending_comparison_source_refs(workspace["source_sets"][0]) == ["S02"]


def test_partial_reading_ledger_is_descriptive_not_a_completion_quota() -> None:
    workspace = {
        "coverage_questions": [],
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:partial",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "sequential",
                "start_offset": 0,
                "end_offset": 2_000,
                "total_chars": 20_000,
                "delivery_status": "delivered",
            }
        ],
    }
    basis, error = validate_reading_basis(
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        representation_status="readable",
        raw_basis={
            "receipt_refs": ["receipt:partial"],
            "coverage_summary": "Read the complete relevant argument section.",
            "unread_regions": "Appendices unrelated to the corpus questions.",
            "unread_risk_assessment": "The appendices cannot change the bounded claim.",
            "representation_limits": "",
            "stopping_reason": "The claim and qualifications were established from the relevant section.",
        },
    )

    assert error is None
    assert basis["receipt_refs"] == ["receipt:partial"]
    assert basis["coverage_ledger"]["unread_char_count"] == 18_000
    assert basis["coverage_ledger"]["completion_is_model_judgment"] is True


def test_finish_reconciliation_detects_receipts_missing_from_latest_analysis() -> None:
    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:new",
                "set_id": "set-1",
                "source_ref": "S01",
                "read_kind": "sequential",
                "delivery_status": "delivered",
            }
        ],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "reading_basis": {"receipt_refs": []},
            }
        ],
    }

    assert tracking.source_refs_with_reads_newer_than_analysis(workspace, set_id="set-1") == ["S01"]
    workspace["source_assessments"][0]["reading_basis"]["receipt_refs"] = ["receipt:new"]
    assert tracking.source_refs_with_reads_newer_than_analysis(workspace, set_id="set-1") == []


def test_source_analysis_tool_does_not_make_model_echo_backend_read_state() -> None:
    async def handler(_args):
        return {"accepted": True}

    parameters = source_analysis_tool(handler).parameters
    assert "question_dispositions" not in parameters["properties"]
    reading_basis = parameters["properties"]["reading_basis"]
    assert "receipt_refs" not in reading_basis["properties"]


def test_receipt_delivery_alone_is_not_semantic_agent_progress() -> None:
    workspace = {
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"], "status": "in_progress"}],
        "source_read_receipts": [],
        "reading_notes": [],
    }
    before = _set_progress_state(workspace, "set-1")
    workspace["source_read_receipts"].append(
        {
            "receipt_ref": "receipt:S01:1",
            "set_id": "set-1",
            "source_ref": "S01",
            "delivery_status": "delivered",
        }
    )
    assert _set_progress_state(workspace, "set-1") == before

    workspace["reading_notes"].append(
        {
            "note_id": "note:S01:1",
            "set_id": "set-1",
            "source_ref": "S01",
            "what_was_read": "The relevant section.",
            "what_was_established": "The bounded claim.",
        }
    )
    assert _set_progress_state(workspace, "set-1") != before


def test_covered_requirement_requires_model_authored_closure_basis() -> None:
    async def handler(_args):
        return {"accepted": True}

    tool = coverage_outcome_tool(handler)
    requirement_schema = tool.parameters["properties"]["requirement_results"]["items"]
    assert "closure_basis" in requirement_schema["required"]
    assert "remaining_uncertainty" in requirement_schema["required"]

    finding = {
        "finding_id": "finding-1",
        "set_id": "set-1",
        "question_links": [{"question_id": "q1", "requirement_ids": ["requirement-1"]}],
    }
    common = {
        "requirement_id": "requirement-1",
        "status": "covered",
        "finding_ids": ["finding-1"],
        "summary": "The requirement is established.",
        "remaining_uncertainty": "",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
    }
    question = {
        "question_id": "q1",
        "answer_requirements": [{"requirement_id": "requirement-1"}],
    }
    _, _, error = SourceTriageWorkspaceToolHandlers._validate_requirement_results(
        question=question,
        raw_results=[{**common, "closure_basis": ""}],
        overall_status="covered",
        findings_by_id={"finding-1": finding},
        set_id="set-1",
        allow_global_next_action=False,
    )
    assert error is not None
    assert "closure_basis" in error["error"]

    results, _, error = SourceTriageWorkspaceToolHandlers._validate_requirement_results(
        question=question,
        raw_results=[
            {
                **common,
                "closure_basis": "The exact finding establishes the requested event and explanatory link.",
            }
        ],
        overall_status="covered",
        findings_by_id={"finding-1": finding},
        set_id="set-1",
        allow_global_next_action=False,
    )
    assert error is None
    assert results[0]["closure_basis"]


@pytest.mark.asyncio
async def test_finalizer_grounding_audit_is_bound_to_current_finding_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finding = {
        "finding_id": "finding-1",
        "set_id": "set-1",
        "source_id": "source-1",
        "source_ref": "S01",
        "statement": "The named actor used the documented mechanism in 1983.",
        "support_explanation": "The exact passage states the actor, mechanism, and period.",
        "evidence_ids": ["evidence-1"],
        "evidence_previews": [
            {
                "evidence_id": "evidence-1",
                "snippet_preview": "The exact supporting passage.",
                "snippet_chars": 29,
                "snippet_truncated": False,
                "locator": "p. 4",
            }
        ],
        "scope": {"actor": "Named actor", "period": "1983", "geography": "", "qualifiers": []},
        "limitations": [],
        "question_links": [{"question_id": "q1", "requirement_ids": ["r1"]}],
    }
    workspace = {
        "source_findings": [finding],
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "coverage_outcomes": [],
    }
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
    )

    assert current_grounding_audit_violations(workspace)
    result = await handler.audit_source_finding_tool(_strict_audit_args(finding_id="finding-1"))
    assert result["accepted"] is True
    assert current_grounding_audit_violations(state["workspace"]) == []

    state["workspace"]["source_findings"][0]["statement"] = "A broader unsupported statement."
    assert current_grounding_audit_violations(state["workspace"])


@pytest.mark.asyncio
async def test_finalizer_can_replace_weak_finding_evidence_after_targeted_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_evidence_id = "evidence-old"
    new_evidence_id = "evidence-new"
    finding = {
        "finding_id": "finding-1",
        "set_id": "set-1",
        "source_id": "source-1",
        "source_ref": "S01",
        "statement": "An overbroad original statement.",
        "support_explanation": "The original attachment was incomplete.",
        "evidence_ids": [old_evidence_id],
        "evidence_previews": [
            {
                "evidence_id": old_evidence_id,
                "snippet_preview": "Weak support.",
                "snippet_chars": 13,
                "snippet_truncated": False,
                "locator": "section 1",
            }
        ],
        "scope": {"actor": "", "period": "", "geography": "", "qualifiers": []},
        "limitations": [],
        "question_links": [{"question_id": "q1", "requirement_ids": ["r1"]}],
    }
    workspace = {
        "source_findings": [finding],
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_id": "source-1",
                "source_ref": "S01",
                "finding_ids": ["finding-1"],
                "evidence_ids": [old_evidence_id],
            }
        ],
        "coverage_outcomes": [],
        "source_triage_finalizer_evidence_reads": [new_evidence_id],
    }
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    evidence = SimpleNamespace(
        id=new_evidence_id,
        source_id="source-1",
        snippet_text="The exact narrower statement is established here.",
        locator_text="section 2",
        metadata_={},
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=_EvidenceToolset({new_evidence_id: evidence}),
    )
    args = _strict_audit_args(finding_id="finding-1", verdict="narrowed")
    args.update(
        {
            "revised_statement": "The exact narrower statement occurred.",
            "revised_support_explanation": "The replacement passage directly establishes it.",
            "revised_evidence_ids": [new_evidence_id],
            "revised_scope": {
                "actor": "The named actor",
                "period": "The documented period",
                "geography": "",
                "qualifiers": [],
            },
        }
    )

    result = await handler.audit_source_finding_tool(args)

    assert result["accepted"] is True
    revised = state["workspace"]["source_findings"][0]
    assert revised["evidence_ids"] == [new_evidence_id]
    assert revised["evidence_previews"][0]["snippet_preview"] == evidence.snippet_text


@pytest.mark.asyncio
async def test_removing_finding_clears_orphaned_positive_legacy_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finding = {
        "finding_id": "finding-1",
        "set_id": "set-1",
        "source_id": "source-1",
        "source_ref": "S01",
        "statement": "Unsupported statement.",
        "support_explanation": "Weak support.",
        "evidence_ids": ["evidence-1"],
        "evidence_previews": [
            {
                "evidence_id": "evidence-1",
                "snippet_preview": "The actual passage.",
                "snippet_chars": 19,
                "snippet_truncated": False,
            }
        ],
        "question_links": [{"question_id": "q1", "requirement_ids": ["r1"]}],
    }
    workspace = {
        "source_findings": [finding],
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_id": "source-1",
                "source_ref": "S01",
                "finding_ids": ["finding-1"],
                "coverage_question_ids": ["q1"],
                "question_dispositions": [
                    {"question_id": "q1", "status": "supports", "rationale": "Legacy positive."},
                    {"question_id": "q2", "status": "irrelevant", "rationale": "Legacy negative."},
                ],
            }
        ],
        "coverage_outcomes": [],
    }
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
    )
    args = _strict_audit_args(finding_id="finding-1", verdict="removed")
    args["source_no_findings_reason"] = "The exact passage does not establish a substantive finding."

    result = await handler.audit_source_finding_tool(args)

    assert result["accepted"] is True
    assessment = state["workspace"]["source_assessments"][0]
    assert assessment["finding_ids"] == []
    assert assessment["coverage_question_ids"] == []
    assert assessment["question_dispositions"] == [
        {"question_id": "q2", "status": "irrelevant", "rationale": "Legacy negative."}
    ]
