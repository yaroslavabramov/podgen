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
from app.generation_v2.source_triage_read_state import (
    canonical_source_read_state,
    merge_source_structures,
    source_read_state_signature,
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
    assert blocked["checkpoint_requirements"] == [
        {
            "source_ref": "S01",
            "receipt_refs": ["receipt:S01:1"],
            "pending_evidence_ids": [],
            "read_ranges": [
                {
                    "receipt_ref": "receipt:S01:1",
                    "read_kind": "sequential",
                    "start_offset": 0,
                    "end_offset": 4_000,
                    "replay": {
                        "tool": "read_source",
                        "source_ref": "S01",
                        "cursor": "",
                        "max_chars": 4_000,
                    },
                }
            ],
            "allowed_next_action": (
                "Call save_reading_note with this exact source_ref and every receipt_ref listed here, "
                "or save a complete source analysis that incorporates these reads."
            ),
        }
    ]

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
                "structure": {
                    "source_ref": "S01",
                    "total_count": 1,
                    "returned_count": 1,
                    "omitted_count": 0,
                    "items": [
                        {
                            "kind": "heading",
                            "label_preview": "Relevant argument",
                            "start_offset": 0,
                            "end_offset": 20_000,
                        }
                    ],
                    "counts": {"headings": 1},
                    "total_chars": 20_000,
                },
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


def test_search_only_read_requires_complete_document_structure() -> None:
    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:search",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "search",
                "start_offset": 4_000,
                "end_offset": 5_000,
                "total_chars": 20_000,
                "delivery_status": "delivered",
                "structure": {
                    "source_ref": "S01",
                    "total_count": 50,
                    "items": [
                        {
                            "kind": "heading",
                            "label_preview": "First",
                            "start_offset": 0,
                            "end_offset": 100,
                        }
                    ],
                    "counts": {"headings": 50},
                    "total_chars": 20_000,
                },
            }
        ],
    }

    basis, error = validate_reading_basis(
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        representation_status="partial",
        raw_basis={
            "coverage_summary": "Targeted locations were read.",
            "unread_regions": "Other sections.",
            "unread_risk_assessment": "Claimed irrelevant.",
            "representation_limits": "",
            "stopping_reason": "Search appeared sufficient.",
        },
    )

    assert basis == {}
    assert error is not None
    assert error["error"] == "partial_read_requires_complete_structure"
    assert error["structure_progress"] == {
        "returned_count": 1,
        "total_count": 50,
        "next_cursor": "1",
    }


def test_search_only_read_is_mechanically_eligible_after_complete_structure() -> None:
    item = {
        "kind": "heading",
        "label_preview": "Only section",
        "start_offset": 0,
        "end_offset": 20_000,
    }
    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:search",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "search",
                "start_offset": 4_000,
                "end_offset": 5_000,
                "total_chars": 20_000,
                "delivery_status": "delivered",
            }
        ],
        "source_structure_pages": [
            {
                "receipt_ref": "structure:1",
                "set_id": "set-1",
                "source_ref": "S01",
                "cursor": "0",
                "total_count": 1,
                "items": [item],
                "counts": {"headings": 1},
                "total_chars": 20_000,
                "delivery_status": "delivered",
            }
        ],
    }

    basis, error = validate_reading_basis(
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        representation_status="partial",
        raw_basis={
            "coverage_summary": "The relevant section was read.",
            "unread_regions": "The remaining mapped section content.",
            "unread_risk_assessment": "The complete structure was checked.",
            "representation_limits": "",
            "stopping_reason": "The bounded finding and structure were checked.",
        },
    )

    assert error is None
    assert basis["coverage_ledger"]["structure"]["complete"] is True


def test_unstructured_partial_source_requires_sequential_completion() -> None:
    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:first",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "sequential",
                "start_offset": 0,
                "end_offset": 20_000,
                "total_chars": 100_000,
                "delivery_status": "delivered",
                "structure": {
                    "source_ref": "S01",
                    "total_count": 5,
                    "returned_count": 5,
                    "omitted_count": 0,
                    "items": [
                        {
                            "kind": "text_range",
                            "label_preview": f"chars {index * 20_000 + 1}-{(index + 1) * 20_000}",
                            "start_offset": index * 20_000,
                            "end_offset": (index + 1) * 20_000,
                        }
                        for index in range(5)
                    ],
                    "counts": {"coarse_text_ranges": 5},
                    "total_chars": 100_000,
                },
            }
        ],
    }

    basis, error = validate_reading_basis(
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        representation_status="readable",
        raw_basis={
            "coverage_summary": "The introduction was read.",
            "unread_regions": "The rest of the unstructured document.",
            "unread_risk_assessment": "Assumed repetitive.",
            "representation_limits": "No headings were extracted.",
            "stopping_reason": "The introduction appeared sufficient.",
        },
    )

    assert basis == {}
    assert error is not None
    assert error["error"] == "unstructured_source_requires_sequential_completion"
    assert error["read_progress"] == {
        "sequential_chars_read": 20_000,
        "total_chars": 100_000,
        "next_cursor": "receipt:first",
    }


def test_verified_exact_duplicate_does_not_require_rereading_identical_remainder() -> None:
    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:first",
                "set_id": "set-1",
                "source_ref": "S02",
                "source_version_id": "version-2",
                "read_kind": "sequential",
                "start_offset": 0,
                "end_offset": 10_000,
                "total_chars": 100_000,
                "delivery_status": "delivered",
                "structure": {
                    "source_ref": "S02",
                    "total_count": 5,
                    "returned_count": 5,
                    "omitted_count": 0,
                    "items": [
                        {
                            "kind": "text_range",
                            "label_preview": "coarse range",
                            "start_offset": 0,
                            "end_offset": 100_000,
                        }
                    ],
                    "counts": {"coarse_text_ranges": 5},
                    "total_chars": 100_000,
                },
            }
        ],
    }

    basis, error = validate_reading_basis(
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S02",
        representation_status="readable",
        verified_exact_duplicate_of_source_ref="S01",
        raw_basis={
            "coverage_summary": "Начало сверено с точным дубликатом S01.",
            "unread_regions": "Остаток байт-в-байт совпадает с S01.",
            "unread_risk_assessment": "Нового содержания в идентичном представлении нет.",
            "representation_limits": "",
            "stopping_reason": "Точное совпадение представления подтверждено системой.",
        },
    )

    assert error is None
    assert basis["verified_exact_duplicate_of_source_ref"] == "S01"


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


def test_repeated_structure_delivery_is_stale_only_when_semantic_coverage_changes() -> None:
    structure_item = {
        "kind": "heading",
        "label_preview": "A",
        "start_offset": 0,
        "end_offset": 100,
    }
    saved_structure = {
        "source_ref": "S01",
        "total_count": 1,
        "items": [structure_item],
        "returned_count": 1,
        "omitted_count": 0,
        "complete": True,
        "next_unread_cursor": "",
        "counts": {"headings": 1},
        "total_chars": 100,
        "delivered_page_count": 1,
    }
    workspace = {
        "source_read_receipts": [],
        "source_structure_pages": [
            {
                "receipt_ref": "structure_receipt:one",
                "set_id": "set-1",
                "source_ref": "S01",
                "cursor": "0",
                "total_count": 1,
                "items": [structure_item],
                "counts": {"headings": 1},
                "total_chars": 100,
                "delivery_status": "delivered",
            },
            {
                "receipt_ref": "structure_receipt:repeat",
                "set_id": "set-1",
                "source_ref": "S01",
                "cursor": "0",
                "total_count": 1,
                "items": [structure_item],
                "counts": {"headings": 1},
                "total_chars": 100,
                "delivery_status": "delivered",
            },
        ],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "reading_basis": {
                    "receipt_refs": [],
                    "structure_receipt_refs": ["structure_receipt:one"],
                    "coverage_ledger": {"structure": saved_structure},
                },
            }
        ],
    }

    assert tracking.source_refs_with_reads_newer_than_analysis(workspace, set_id="set-1") == []
    workspace["source_structure_pages"][1]["total_count"] = 2
    workspace["source_structure_pages"][1]["items"].append(
        {
            "kind": "heading",
            "label_preview": "B",
            "start_offset": 100,
            "end_offset": 200,
        }
    )
    assert tracking.source_refs_with_reads_newer_than_analysis(workspace, set_id="set-1") == ["S01"]


def test_receipt_embedded_structure_is_current_immediately_after_analysis() -> None:
    structure = {
        "source_ref": "S01",
        "total_count": 1,
        "items": [{"kind": "heading", "label_preview": "A", "start_offset": 0, "end_offset": 100}],
        "counts": {"headings": 1},
        "total_chars": 100,
    }
    workspace = {
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:one",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "content_sha256": "content-1",
                "read_kind": "sequential",
                "start_offset": 0,
                "end_offset": 100,
                "total_chars": 100,
                "eof": True,
                "structure": structure,
                "delivery_status": "delivered",
            }
        ],
        "source_structure_pages": [],
        "source_assessments": [],
    }
    basis, error = validate_reading_basis(
        workspace=workspace,
        source_set={"set_id": "set-1"},
        source_ref="S01",
        representation_status="readable",
        raw_basis={
            "coverage_summary": "The full source was read.",
            "unread_regions": "None.",
            "unread_risk_assessment": "No unread material.",
            "representation_limits": "",
            "stopping_reason": "EOF was reached.",
        },
    )
    assert error is None
    workspace["source_assessments"] = [
        {"set_id": "set-1", "source_ref": "S01", "reading_basis": basis}
    ]
    assert tracking.source_refs_with_reads_newer_than_analysis(workspace, set_id="set-1") == []


def test_structure_merge_is_independent_of_delivery_path_and_duplicates() -> None:
    item = {"kind": "heading", "label_preview": "A", "start_offset": 0, "end_offset": 100}
    receipt_structure = {
        "source_ref": "S01",
        "total_count": 1,
        "items": [item],
        "counts": {"headings": 1},
        "total_chars": 100,
    }
    page_structure = {
        **receipt_structure,
        "returned_count": 1,
        "omitted_count": 0,
        "complete": True,
        "next_unread_cursor": "",
        "delivered_page_count": 1,
    }
    receipt_only = merge_source_structures([receipt_structure], source_ref="S01")
    page_only = merge_source_structures([page_structure], source_ref="S01")
    both = merge_source_structures(
        [receipt_structure, page_structure, page_structure],
        source_ref="S01",
    )
    assert receipt_only == page_only == both


def test_legacy_embedded_structure_uses_omitted_count_instead_of_claiming_complete() -> None:
    items = [
        {
            "kind": "heading",
            "label_preview": f"Section {index}",
            "start_offset": index * 100,
            "end_offset": (index + 1) * 100,
        }
        for index in range(3)
    ]

    merged = merge_source_structures(
        [
            {
                "items": items,
                "returned_count": 3,
                "omitted_count": 7,
                "counts": {"headings": 10},
            }
        ],
        source_ref="S01",
    )

    assert merged["total_count"] == 10
    assert merged["returned_count"] == 3
    assert merged["complete"] is False
    assert merged["next_unread_cursor"] == "3"


def test_duplicate_semantic_receipt_does_not_change_canonical_read_state() -> None:
    receipt = {
        "set_id": "set-1",
        "source_ref": "S01",
        "source_version_id": "version-1",
        "content_sha256": "content-1",
        "read_kind": "sequential",
        "start_offset": 0,
        "end_offset": 100,
        "total_chars": 100,
        "delivery_status": "delivered",
    }
    first = canonical_source_read_state(
        {"source_read_receipts": [{**receipt, "receipt_ref": "receipt:one"}]},
        set_id="set-1",
        source_ref="S01",
    )
    repeated = canonical_source_read_state(
        {
            "source_read_receipts": [
                {**receipt, "receipt_ref": "receipt:one"},
                {**receipt, "receipt_ref": "receipt:duplicate"},
            ]
        },
        set_id="set-1",
        source_ref="S01",
    )
    assert first == repeated


def test_same_text_receipt_from_another_query_does_not_change_signature() -> None:
    receipt = {
        "set_id": "set-1",
        "source_ref": "S01",
        "source_version_id": "version-1",
        "content_sha256": "content-1",
        "read_kind": "search",
        "start_offset": 100,
        "end_offset": 300,
        "total_chars": 1_000,
        "delivery_status": "delivered",
    }
    first = {
        "source_read_receipts": [
            {**receipt, "receipt_ref": "receipt:one", "request": {"query": "first term"}}
        ]
    }
    repeated = {
        "source_read_receipts": [
            *first["source_read_receipts"],
            {
                **receipt,
                "receipt_ref": "receipt:two",
                "read_kind": "sequential",
                "request": {"query": "second term"},
            },
        ]
    }
    assert source_read_state_signature(first, set_id="set-1", source_ref="S01") == (
        source_read_state_signature(repeated, set_id="set-1", source_ref="S01")
    )


def test_new_exact_read_remains_stale_until_source_analysis_acknowledges_it() -> None:
    workspace = {
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "pending_exact_reads": [
                    {
                        "source_ref": "S01",
                        "evidence_ids": ["evidence:new"],
                        "updated_at": "2026-07-20T10:00:00",
                    }
                ],
            }
        ],
        "source_read_receipts": [],
        "source_assessments": [],
        "reading_notes": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "what_was_read": "The new exact window.",
                "what_was_established": "Its bounded meaning.",
                "updated_at": "2026-07-20T10:01:00",
            }
        ],
    }
    acknowledged_signature = source_read_state_signature(
        workspace,
        set_id="set-1",
        source_ref="S01",
        acknowledge_pending=True,
    )
    workspace["source_assessments"] = [
        {
            "set_id": "set-1",
            "source_ref": "S01",
            "reading_basis": {"read_state_signature": acknowledged_signature},
        }
    ]

    assert tracking.pending_exact_read_source_refs(
        workspace,
        source_set=workspace["source_sets"][0],
    ) == ["S01"]
    assert tracking.source_refs_with_reads_newer_than_analysis(workspace, set_id="set-1") == ["S01"]

    tracking.clear_pending_exact_read(workspace["source_sets"][0], source_ref="S01")
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
