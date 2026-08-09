from __future__ import annotations

from types import SimpleNamespace
import inspect

import pytest

from app.generation_v2 import triage_semantic_eval_runner
from app.generation_v2.source_triage_workspace_prompting import source_set_prompt_payload
from app.generation_v2.runtime_tool_calls import _validated_private_runtime_directive
from app.generation_v2.triage_semantic_eval_runner import semantic_eval_report
from app.generation_v2.triage_semantic_eval_state import (
    build_semantic_eval_workspace,
    semantic_eval_evidence_ids,
)


def _source_workspace(long_text: str = "") -> dict:
    return {
        "schema_version": "source_triage_workspace.v3",
        "_workspace_revision": 11,
        "source_order": ["S01"],
        "intent": {"language": "ru"},
        "central_question_id": "root",
        "coverage_questions": [
            {
                "question_id": "root",
                "question": "Почему сохранилась система?",
                "answer_requirements": [
                    {"requirement_id": "root:history", "completion_criterion": "Есть хронология."},
                    {"requirement_id": "root:cause", "completion_criterion": "Есть причина."},
                ],
            },
            {
                "question_id": "origin",
                "parent_question_id": "root",
                "question": "Как возникло право?",
                "answer_requirements": [
                    {"requirement_id": "origin:mechanism", "completion_criterion": "Есть механизм."}
                ],
            },
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "in_progress",
                "source_refs": ["S01"],
                "question_ids": ["root", "origin"],
                "planned_question_ids": ["root", "origin"],
                "question_assignments": [],
                "evidence_ids": ["old-evidence"],
                "pending_exact_reads": [{"source_ref": "S01"}],
            }
        ],
        "source_assessments": [
            {
                "set_id": "set-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "processing_status": "resolved",
                "finding_ids": ["finding-1"],
                "evidence_ids": ["old-evidence"],
                "source_summary": long_text,
                "reading_basis": {"read_state_signature": "stable"},
            }
        ],
        "source_findings": [
            {
                "set_id": "set-1",
                "finding_id": "finding-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "statement": long_text or "Установлен ограниченный исторический факт.",
                "evidence_ids": ["old-evidence"],
                "evidence_previews": [{"evidence_id": "old-evidence", "snippet_preview": "quote"}],
                "question_links": [
                    {"question_id": "root", "requirement_ids": ["root:cause"]}
                ],
                "possible_question_ids": ["root", "origin"],
                "source_relations": [{"kind": "supports", "evidence_ids": ["old-evidence"]}],
            }
        ],
        "source_read_receipts": [
            {"set_id": "set-1", "source_ref": "S01", "receipt_ref": "receipt-1"}
        ],
        "reading_notes": [{"set_id": "set-1", "source_ref": "S01", "note_id": "note-1"}],
        "source_structure_pages": [],
        "coverage_outcomes": [{"set_id": "set-1", "question_id": "root", "status": "covered"}],
        "coverage_syntheses": [{"set_id": "set-1", "question_id": "root"}],
    }


def test_semantic_eval_state_keeps_full_reading_memory_but_removes_old_decisions() -> None:
    long_text = "Д" * 20_000
    source = _source_workspace(long_text)
    fresh = {
        "schema_version": "source_triage_workspace.v3",
        "_workspace_revision": 3,
        "source_order": ["S01"],
        "intent": {"language": "ru"},
    }

    workspace = build_semantic_eval_workspace(
        fresh_workspace=fresh,
        source_workspace=source,
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )

    source_set = workspace["source_sets"][0]
    assert source_set["status"] == "planned"
    assert workspace["triage_semantic_eval"]["mode"] == "frozen_post_reading_state"
    assert "pending_exact_reads" not in source_set
    assert source_set["question_assignments"] == [
        {"question_id": "root", "requirement_ids": ["root:history", "root:cause"]},
        {"question_id": "origin", "requirement_ids": ["origin:mechanism"]},
    ]
    assert workspace["coverage_outcomes"] == []
    assert workspace["coverage_syntheses"] == []
    assert workspace["source_findings"][0]["question_links"] == []
    assert workspace["source_findings"][0]["source_relations"][0]["evidence_ids"] == [
        "new-evidence"
    ]
    assert workspace["source_findings"][0]["evidence_previews"][0]["evidence_id"] == (
        "new-evidence"
    )
    assert semantic_eval_evidence_ids(workspace) == ["new-evidence"]
    assert workspace["source_assessments"][0]["source_summary"] == long_text
    assert workspace["source_findings"][0]["statement"] == long_text


def test_semantic_eval_prompt_payload_declares_frozen_decision_mode() -> None:
    workspace = build_semantic_eval_workspace(
        fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
        source_workspace=_source_workspace(),
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )

    payload = source_set_prompt_payload(workspace, "set-1")

    assert payload["set"]["execution_mode"] == "semantic_eval"
    assert "frozen" in payload["set"]["execution_goal"]
    assert "do not read sources" in payload["set"]["execution_goal"]


def test_semantic_eval_runner_does_not_call_legacy_set_agent() -> None:
    source = inspect.getsource(triage_semantic_eval_runner)

    assert "run_source_triage_set" not in source
    assert "save_coverage_hint" not in source


def test_semantic_eval_state_fails_closed_when_evidence_clone_is_incomplete() -> None:
    with pytest.raises(ValueError, match="evidence clone is incomplete"):
        build_semantic_eval_workspace(
            fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
            source_workspace=_source_workspace(),
            set_id="set-1",
            question_ids=["root", "origin"],
            evidence_id_map={},
        )


def test_finding_catalog_page_can_schedule_same_chain_delivery() -> None:
    messages, receipt_refs, error = _validated_private_runtime_directive(
        tool_name="read_triage_set_state",
        output={
            "accepted": True,
            "section": "finding_catalog",
            "finding_catalog_receipt_ref": "finding_catalog_receipt:1234567890abcdef",
        },
        raw_runtime_messages=[],
        raw_receipt_delivery={
            "receipt_refs": ["finding_catalog_receipt:1234567890abcdef"]
        },
        has_runtime_messages=False,
        has_receipt_delivery=True,
    )

    assert error is None
    assert messages == []
    assert receipt_refs == ["finding_catalog_receipt:1234567890abcdef"]


def test_semantic_eval_report_requires_mapping_hints_without_rereading_or_outcomes() -> None:
    workspace = build_semantic_eval_workspace(
        fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
        source_workspace=_source_workspace(),
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )
    source_set = workspace["source_sets"][0]
    source_set["status"] = "mapped"
    workspace["coverage_hints"] = [
        {
            "hint_id": "hint:root:set-1",
            "set_id": "set-1",
            "source_ref": "S01",
            "question_id": "root",
            "requirement_ids": ["root:cause"],
            "candidate_finding_ids": ["finding-1"],
            "candidate_evidence_ids": ["new-evidence"],
            "observed_gap": "",
            "conflict_signal": "",
            "notes": ["Находка указывает на причинный критерий, но не является ответом."],
        },
        {
            "hint_id": "hint:origin:set-1",
            "set_id": "set-1",
            "source_ref": "S01",
            "question_id": "origin",
            "requirement_ids": ["origin:mechanism"],
            "candidate_finding_ids": ["finding-1"],
            "candidate_evidence_ids": ["new-evidence"],
            "observed_gap": "",
            "conflict_signal": "",
            "notes": ["Находка указывает на критерий происхождения."],
        },
    ]
    traces = [
        SimpleNamespace(
            tool_name="finish_triage_source_set",
            output_json={"accepted": True},
        )
    ]

    report = semantic_eval_report(
        workspace=workspace,
        source_set=source_set,
        traces=traces,
        question_ids=["root", "origin"],
        expected_question_statuses={"root": "hinted", "origin": "hinted"},
        expected_requirement_statuses={
            ("root", "root:cause"): "hinted",
            ("origin", "origin:mechanism"): "hinted",
        },
    )

    assert report["passed"] is True
    assert report["source_read_call_count"] == 0
    assert report["accepted_finish_count"] == 1
    assert report["coverage_hint_count"] == 2
    assert "outcomes" not in report


def test_semantic_eval_report_checks_hk10_salient_fact_mapping() -> None:
    fact_text = (
        "HKMA restored the HK$10 note after an attempted coin replacement because of public demand."
    )
    workspace = build_semantic_eval_workspace(
        fresh_workspace={"source_order": ["S01"], "intent": {"language": "ru"}},
        source_workspace=_source_workspace(fact_text),
        set_id="set-1",
        question_ids=["root", "origin"],
        evidence_id_map={"old-evidence": "new-evidence"},
    )
    source_set = workspace["source_sets"][0]
    source_set["status"] = "mapped"
    traces = [
        SimpleNamespace(
            tool_name="finish_triage_source_set",
            output_json={"accepted": True},
        )
    ]
    expected_salient_facts = [
        {
            "label": "hk10_public_demand_hkma",
            "question_id": "root",
            "requirement_id": "root:cause",
            "source_ref": "S01",
            "required_terms": ["HK$10", "public demand", "HKMA"],
        }
    ]

    missing = semantic_eval_report(
        workspace=workspace,
        source_set=source_set,
        traces=traces,
        question_ids=["root"],
        expected_question_statuses={},
        expected_requirement_statuses={},
        expected_salient_facts=expected_salient_facts,
    )
    workspace["coverage_hints"] = [
        {
            "hint_id": "hint:hk10",
            "set_id": "set-1",
            "source_ref": "S01",
            "question_id": "root",
            "requirement_ids": ["root:cause"],
            "candidate_finding_ids": ["finding-1"],
            "candidate_evidence_ids": ["new-evidence"],
            "observed_gap": "",
            "conflict_signal": "",
            "notes": ["Use this as a research lead, not as a final answer."],
        }
    ]
    mapped = semantic_eval_report(
        workspace=workspace,
        source_set=source_set,
        traces=traces,
        question_ids=["root"],
        expected_question_statuses={},
        expected_requirement_statuses={},
        expected_salient_facts=expected_salient_facts,
    )

    assert missing["passed"] is False
    assert missing["salient_facts"][0]["passed"] is False
    assert mapped["passed"] is True
    assert mapped["salient_facts"][0]["passed"] is True


def test_semantic_eval_report_checks_source_analysis_salient_fact_without_hints() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "set-1", "status": "mapped", "source_refs": ["S03"], "question_ids": ["current"]}
        ],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": "finding:S03:hk10",
                        "statement": (
                            "HKMA restored the HK$10 note after attempted coin replacement because "
                            "public demand continued."
                        ),
                        "question_ids": ["current"],
                        "evidence_ids": ["evidence-hk10"],
                        "limitations": [],
                    }
                ],
                "gaps": [],
            }
        ],
    }
    source_set = workspace["source_sets"][0]

    report = semantic_eval_report(
        workspace=workspace,
        source_set=source_set,
        traces=[],
        question_ids=["current"],
        expected_question_statuses={"current": "mapped"},
        expected_requirement_statuses={},
        expected_salient_facts=[
            {
                "label": "hk10_public_demand_hkma",
                "question_id": "current",
                "source_ref": "S03",
                "required_terms": ["HK$10", "public demand", "HKMA"],
            }
        ],
    )

    assert report["passed"] is True
    assert report["source_analysis_count"] == 1
    assert report["coverage_hint_count"] == 0
    assert report["accepted_finish_count"] == 0


def test_semantic_eval_report_requires_salient_fact_in_one_source_analysis_finding() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "set-1", "status": "mapped", "source_refs": ["S03"], "question_ids": ["current"]}
        ],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": "finding:S03:hk10",
                        "statement": "The source says HK$10 notes returned because of public demand.",
                        "question_ids": ["current"],
                        "evidence_ids": ["evidence-hk10"],
                        "limitations": [],
                    },
                    {
                        "finding_id": "finding:S03:demand",
                        "statement": "The source separately describes a later banknote programme.",
                        "question_ids": ["current"],
                        "evidence_ids": ["evidence-programme"],
                        "limitations": [],
                    },
                ],
                "gaps": [
                    {
                        "question_id": "current",
                        "description": "HKMA appears in a gap, not in a salient finding.",
                    }
                ],
            }
        ],
    }

    report = semantic_eval_report(
        workspace=workspace,
        source_set=workspace["source_sets"][0],
        traces=[],
        question_ids=["current"],
        expected_question_statuses={},
        expected_requirement_statuses={},
        expected_salient_facts=[
            {
                "label": "hk10_public_demand_hkma",
                "question_id": "current",
                "source_ref": "S03",
                "required_terms": ["HK$10", "public demand", "HKMA"],
            }
        ],
    )

    assert report["passed"] is False
    assert report["salient_facts"][0]["missing_terms"] == ["hkma"]


def test_semantic_eval_report_requires_explicit_expectations() -> None:
    workspace = {
        "source_sets": [{"set_id": "set-1", "status": "mapped", "source_refs": ["S03"]}],
        "source_analyses": [],
    }

    report = semantic_eval_report(
        workspace=workspace,
        source_set=workspace["source_sets"][0],
        traces=[],
        question_ids=["current"],
        expected_question_statuses={},
        expected_requirement_statuses={},
    )

    assert report["passed"] is False
    assert "semantic eval requires at least one explicit expectation" in report["issues"]


def test_semantic_eval_report_does_not_satisfy_salient_fact_from_limitations() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "set-1", "status": "mapped", "source_refs": ["S03"], "question_ids": ["current"]}
        ],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [
                    {
                        "finding_id": "finding:S03:hk10",
                        "statement": "The source says HK$10 notes returned because of public demand.",
                        "question_ids": ["current"],
                        "evidence_ids": ["evidence-hk10"],
                        "limitations": ["The source does not say HKMA caused this demand."],
                    }
                ],
                "gaps": [],
            }
        ],
    }

    report = semantic_eval_report(
        workspace=workspace,
        source_set=workspace["source_sets"][0],
        traces=[],
        question_ids=["current"],
        expected_question_statuses={},
        expected_requirement_statuses={},
        expected_salient_facts=[
            {
                "label": "hk10_public_demand_hkma",
                "question_id": "current",
                "source_ref": "S03",
                "required_terms": ["HK$10", "public demand", "HKMA"],
            }
        ],
    )

    assert report["passed"] is False
    assert report["salient_facts"][0]["missing_terms"] == ["hkma"]


def test_semantic_eval_report_checks_expected_source_specific_gap() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "set-1", "status": "mapped", "source_refs": ["S03"], "question_ids": ["origin"]}
        ],
        "source_analyses": [
            {
                "analysis_id": "analysis:S03",
                "source_ref": "S03",
                "status": "usable",
                "current": True,
                "accepted": True,
                "findings": [],
                "gaps": [
                    {
                        "question_id": "origin",
                        "description": "The source describes practice but not the early legal basis.",
                    }
                ],
            }
        ],
    }

    report = semantic_eval_report(
        workspace=workspace,
        source_set=workspace["source_sets"][0],
        traces=[],
        question_ids=["origin"],
        expected_question_statuses={},
        expected_requirement_statuses={},
        expected_gaps=[
            {
                "label": "early_legal_basis_gap",
                "question_id": "origin",
                "source_ref": "S03",
                "required_terms": ["practice", "early legal basis"],
            }
        ],
    )

    assert report["passed"] is True
    assert report["expected_gaps"][0]["passed"] is True
