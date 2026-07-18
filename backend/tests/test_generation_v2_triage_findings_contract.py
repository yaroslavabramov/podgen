from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.generation_v2.corpus_brief_preview import corpus_brief_preview
from app.generation_v2.research_workspace import _jobs_from_corpus_brief
from app.generation_v2.research_workspace_prompt_payloads import current_job_prompt_payload
from app.generation_v2.research_workspace_tools import _full_corpus_brief_page
from app.generation_v2.schemas_source import (
    CorpusBrief,
    CorpusBriefIntent,
    CoverageOutcome,
    CoverageQuestion,
    CoverageRequirement,
    CoverageRequirementResult,
    FindingQuestionLink,
    FindingSourceRelation,
    SourceCard,
    SourceAssessment,
    SourceFinding,
    SourceMap,
)
from app.generation_v2.source_triage_canonical import (
    audit_corpus_brief,
    materialize_corpus_brief,
    materialize_source_map_v2,
)
from app.generation_v2.source_triage_repair_state import reconcile_carried_triage_evidence


def _inventory() -> SourceMap:
    return SourceMap(
        source_cards=[
            SourceCard(
                source_id="source-1",
                source_version_ids=["version-1"],
                evidence_ids=["evidence-1"],
                title="Source one",
                source_type="web",
                summary="A source.",
            )
        ]
    )


def _questions() -> list[CoverageQuestion]:
    return [
        CoverageQuestion(
            question_id="q_mechanism",
            question="How does it work?",
            answer_requirements=[
                CoverageRequirement(
                    requirement_id="r_actor",
                    description="Identify the responsible actor.",
                ),
                CoverageRequirement(
                    requirement_id="r_mechanism",
                    description="Explain the mechanism.",
                ),
            ],
        ),
        CoverageQuestion(
            question_id="q_effect",
            question="What did it change?",
            answer_requirements=[
                CoverageRequirement(
                    requirement_id="r_effect",
                    description="State the observed effect.",
                )
            ],
        ),
    ]


def _finding(*, statement: str = "The institution used the mechanism and changed the outcome.") -> SourceFinding:
    return SourceFinding(
        finding_id="finding-1",
        source_id="source-1",
        source_version_ids=["version-1"],
        assessment_id="assessment-1",
        statement=statement,
        evidence_ids=["evidence-1"],
        scope={
            "actor": "The institution",
            "period": "1983-1984",
            "qualifiers": ["The evidence covers the initial implementation only."],
        },
        limitations=["Later implementation is outside this source."],
        question_links=[
            FindingQuestionLink(
                question_id="q_mechanism",
                requirement_ids=["r_actor", "r_mechanism"],
            ),
            FindingQuestionLink(
                question_id="q_effect",
                requirement_ids=["r_effect"],
            ),
        ],
        semantic_revision=3,
    )


def test_finding_can_support_multiple_questions_and_requirements_losslessly() -> None:
    long_statement = "Full source-grounded finding. " * 1_000
    source_map = SourceMap(
        schema_version="source_map.v2",
        coverage_questions=_questions(),
        source_findings=[_finding(statement=long_statement)],
        coverage_outcomes=[
            CoverageOutcome(
                outcome_id="outcome-mechanism",
                question_id="q_mechanism",
                status="covered",
                finding_ids=["finding-1"],
                requirement_results=[
                    CoverageRequirementResult(
                        requirement_id="r_actor",
                        status="covered",
                        finding_ids=["finding-1"],
                        summary="The actor is identified.",
                    ),
                    CoverageRequirementResult(
                        requirement_id="r_mechanism",
                        status="covered",
                        finding_ids=["finding-1"],
                        summary="The mechanism is explained.",
                    ),
                ],
                semantic_revision=3,
            )
        ],
    )

    restored = SourceMap.model_validate(source_map.model_dump(mode="json"))

    assert restored.source_findings[0].statement == long_statement
    assert [link.question_id for link in restored.source_findings[0].question_links] == [
        "q_mechanism",
        "q_effect",
    ]
    assert restored.coverage_outcomes[0].requirement_results[1].finding_ids == ["finding-1"]
    assert restored.coverage_outcomes[0].source_ids == ["source-1"]
    assert restored.coverage_outcomes[0].evidence_ids == ["evidence-1"]


def test_zero_findings_reason_survives_source_map_artifact() -> None:
    source_map = SourceMap(
        schema_version="source_map.v2",
        source_assessments=[
            SourceAssessment(
                assessment_id="assessment-1",
                source_id="source-1",
                finding_ids=[],
                no_findings_reason=(
                    "The readable source contains only navigation and no material relevant to any question."
                ),
            )
        ],
    )

    restored = SourceMap.model_validate(source_map.model_dump(mode="json"))

    assert restored.source_assessments[0].finding_ids == []
    assert restored.source_assessments[0].no_findings_reason.startswith("The readable source")


def test_different_findings_from_one_source_support_different_answer_parts() -> None:
    question = _questions()[0]
    actor_finding = _finding().model_copy(
        update={
            "finding_id": "finding-actor",
            "statement": "The institution is the responsible actor.",
            "evidence_ids": ["evidence-actor"],
            "question_links": [
                FindingQuestionLink(
                    question_id="q_mechanism",
                    requirement_ids=["r_actor"],
                )
            ],
        }
    )
    mechanism_finding = _finding().model_copy(
        update={
            "finding_id": "finding-mechanism",
            "statement": "The source describes the operating mechanism.",
            "evidence_ids": ["evidence-mechanism"],
            "question_links": [
                FindingQuestionLink(
                    question_id="q_mechanism",
                    requirement_ids=["r_mechanism"],
                )
            ],
        }
    )
    source_map = SourceMap(
        coverage_questions=[question],
        source_findings=[actor_finding, mechanism_finding],
        coverage_outcomes=[
            CoverageOutcome(
                outcome_id="outcome-mechanism",
                question_id="q_mechanism",
                status="covered",
                requirement_results=[
                    CoverageRequirementResult(
                        requirement_id="r_actor",
                        status="covered",
                        finding_ids=["finding-actor"],
                        summary="The actor is established.",
                    ),
                    CoverageRequirementResult(
                        requirement_id="r_mechanism",
                        status="covered",
                        finding_ids=["finding-mechanism"],
                        summary="The mechanism is established.",
                    ),
                ],
            )
        ],
    )

    outcome = source_map.coverage_outcomes[0]

    assert outcome.finding_ids == ["finding-actor", "finding-mechanism"]
    assert outcome.source_ids == ["source-1"]
    assert outcome.evidence_ids == ["evidence-actor", "evidence-mechanism"]


def test_structured_outcome_rejects_extra_top_level_finding() -> None:
    actor_finding = _finding().model_copy(
        update={
            "finding_id": "finding-actor",
            "question_links": [
                FindingQuestionLink(
                    question_id="q_mechanism",
                    requirement_ids=["r_actor"],
                )
            ],
        }
    )
    mechanism_finding = _finding().model_copy(
        update={
            "finding_id": "finding-mechanism",
            "question_links": [
                FindingQuestionLink(
                    question_id="q_mechanism",
                    requirement_ids=["r_mechanism"],
                )
            ],
        }
    )

    with pytest.raises(ValidationError, match="exactly match requirement result findings"):
        SourceMap(
            coverage_questions=[_questions()[0]],
            source_findings=[actor_finding, mechanism_finding],
            coverage_outcomes=[
                CoverageOutcome(
                    outcome_id="outcome-mechanism",
                    question_id="q_mechanism",
                    status="partial",
                    finding_ids=["finding-actor", "finding-mechanism"],
                    requirement_results=[
                        CoverageRequirementResult(
                            requirement_id="r_actor",
                            status="covered",
                            finding_ids=["finding-actor"],
                            summary="The actor is established.",
                        ),
                        CoverageRequirementResult(
                            requirement_id="r_mechanism",
                            status="missing",
                            missing_detail="The mechanism is not established.",
                        ),
                    ],
                )
            ],
        )


def test_requirement_result_rejects_finding_not_linked_to_that_requirement() -> None:
    payload = SourceMap(
        coverage_questions=_questions(),
        source_findings=[_finding()],
    ).model_dump(mode="json")
    payload["source_findings"].append(
        {
            "finding_id": "finding-question-only",
            "source_id": "source-1",
            "statement": "This finding is linked only to the question as a whole.",
            "evidence_ids": ["evidence-2"],
            "question_links": [{"question_id": "q_effect", "requirement_ids": []}],
        }
    )
    payload["coverage_outcomes"] = [
        {
            "outcome_id": "outcome-effect",
            "question_id": "q_effect",
            "status": "covered",
            "finding_ids": ["finding-question-only"],
            "requirement_results": [
                {
                    "requirement_id": "r_effect",
                    "status": "covered",
                    "finding_ids": ["finding-question-only"],
                }
            ],
        }
    ]

    with pytest.raises(ValidationError, match="not linked to its requirement"):
        SourceMap.model_validate(payload)


def test_outcome_rejects_finding_not_linked_to_its_question() -> None:
    payload = SourceMap(
        coverage_questions=_questions(),
        source_findings=[
            _finding().model_copy(update={"question_links": [_finding().question_links[0]]})
        ],
    ).model_dump(mode="json")
    payload["coverage_outcomes"] = [
        {
            "outcome_id": "outcome-effect",
            "question_id": "q_effect",
            "status": "covered",
            "finding_ids": ["finding-1"],
            "requirement_results": [
                {
                    "requirement_id": "r_effect",
                    "status": "covered",
                    "finding_ids": ["finding-1"],
                    "summary": "The effect is claimed.",
                }
            ],
        }
    ]

    with pytest.raises(ValidationError, match="not linked to its question"):
        SourceMap.model_validate(payload)


def test_materialization_derives_outcome_sources_and_evidence_from_findings() -> None:
    inventory = _inventory()
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "intent": {"user_prompt": "Explain it."},
        "source_order": ["S01"],
        "coverage_questions": [item.model_dump(mode="json") for item in _questions()[:1]],
        "source_sets": [
            {
                "set_id": "set-1",
                "label": "Set one",
                "purpose": "Read source one.",
                "source_refs": ["S01"],
                "question_ids": ["q_mechanism"],
                "question_assignments": [
                    {
                        "question_id": "q_mechanism",
                        "requirement_ids": ["r_actor", "r_mechanism"],
                    }
                ],
                "status": "mapped",
            }
        ],
        "source_assessments": [
            {
                "assessment_id": "assessment-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "source_version_ids": ["version-1"],
                "representation_status": "readable",
                "relevance": "core",
                "authority": "official",
                "uniqueness": "unique",
                "disposition": "anchor",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "source_findings": [
            _finding()
            .model_copy(update={"question_links": [_finding().question_links[0]]})
            .model_dump(mode="json")
        ],
        "coverage_outcomes": [
            {
                "outcome_id": "local-outcome",
                "set_id": "set-1",
                "question_id": "q_mechanism",
                "status": "covered",
                "finding_ids": ["finding-1"],
                "requirement_results": [
                    {
                        "requirement_id": "r_actor",
                        "status": "covered",
                        "finding_ids": ["finding-1"],
                    },
                    {
                        "requirement_id": "r_mechanism",
                        "status": "covered",
                        "finding_ids": ["finding-1"],
                    },
                ],
                "orientation_summary": "The mechanism is covered.",
                "next_action": "research",
                "semantic_revision": 3,
            }
        ],
    }

    source_map = materialize_source_map_v2(
        inventory,
        workspace=workspace,
        inventory=inventory,
    )
    brief = materialize_corpus_brief(
        generation_run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        run_corpus_view_id=str(uuid.uuid4()),
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id=str(uuid.uuid4()),
        source_map=source_map,
        workspace=workspace,
    )

    outcome = brief.coverage_outcomes[0]
    assert outcome.finding_ids == ["finding-1"]
    assert outcome.source_ids == ["source-1"]
    assert outcome.evidence_ids == ["evidence-1"]
    assert source_map.source_assessments[0].finding_ids == ["finding-1"]
    assert brief.source_sets[0].question_assignments[0].requirement_ids == [
        "r_actor",
        "r_mechanism",
    ]
    assert brief.research_handoff[0].finding_ids == ["finding-1"]
    assert brief.research_handoff[0].question_assignments[0].requirement_ids == [
        "r_actor",
        "r_mechanism",
    ]
    audit = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-1"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )
    assert audit.passed is True


def test_research_handoff_persists_full_findings_and_uses_separate_previews() -> None:
    long_statement = "Unabridged finding. " * 1_000
    questions = _questions()[:1]
    finding = _finding(statement=long_statement).model_copy(
        update={"question_links": [_finding().question_links[0]]}
    )
    outcome = CoverageOutcome(
        outcome_id="outcome-mechanism",
        question_id="q_mechanism",
        status="partial",
        source_ids=["source-1"],
        evidence_ids=["evidence-1"],
        finding_ids=["finding-1"],
        requirement_results=[
            CoverageRequirementResult(
                requirement_id="r_actor",
                status="covered",
                finding_ids=["finding-1"],
                summary="The actor is known.",
            ),
            CoverageRequirementResult(
                requirement_id="r_mechanism",
                status="partial",
                finding_ids=["finding-1"],
                summary="Part of the mechanism is known.",
                missing_detail="The final transition remains open.",
            ),
        ],
        orientation_summary="The mechanism is partly established.",
        missing_detail="The final transition remains open.",
        semantic_revision=3,
    )
    brief = CorpusBrief(
        schema_version="corpus_brief.v2",
        generation_run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        run_corpus_view_id=str(uuid.uuid4()),
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id=str(uuid.uuid4()),
        source_map_hash="hash",
        intent=CorpusBriefIntent(user_prompt="Explain it."),
        coverage_questions=questions,
        source_findings=[finding],
        coverage_outcomes=[outcome],
        research_handoff=[
            {
                "handoff_id": "handoff-1",
                "objective": "Finish the mechanism.",
                "question_ids": ["q_mechanism"],
                "question_assignments": [
                    {"question_id": "q_mechanism", "requirement_ids": ["r_mechanism"]}
                ],
                "finding_ids": ["finding-1"],
                "preferred_source_ids": ["source-1"],
                "starting_evidence_ids": ["evidence-1"],
            }
        ],
    )

    job = _jobs_from_corpus_brief(brief, _inventory(), allowed_source_refs=["S01"])[0]
    payload = current_job_prompt_payload(job)
    brief_preview = corpus_brief_preview(brief, source_inventory=_inventory(), text_limit=120)

    assert job["source_findings"][0]["statement"] == long_statement
    assert job["corpus_handoff_finding_ids"] == ["finding-1"]
    assert job["corpus_handoff_question_assignments"][0]["requirement_ids"] == [
        "r_mechanism"
    ]
    assert [item["requirement_id"] for item in job["open_requirements"]] == ["r_mechanism"]
    assert job["starting_evidence_ids"] == ["evidence-1"]
    finding_preview = payload["corpus_brief_lineage"]["source_finding_previews"][0]
    assert finding_preview["statement_preview"] == long_statement[:700]
    assert finding_preview["statement_truncated"] is True
    assert payload["pages"]["source_findings"]["full_section"] == "corpus_brief_findings"
    assert brief_preview["source_findings"][0]["statement"]["preview"] == long_statement[:120]
    assert brief.source_findings[0].statement == long_statement
    full_page = _full_corpus_brief_page(
        {"corpus_brief": brief.model_dump(mode="json")},
        _inventory(),
        section="corpus_brief_findings",
        cursor=0,
        limit=20,
        include_evidence_ids=True,
    )
    assert full_page["accepted"] is False
    assert full_page["error"] == "workspace_item_exceeds_model_read_contract"

    short_payload = brief.model_dump(mode="json")
    short_payload["source_findings"][0]["statement"] = "A complete short finding."
    readable_page = _full_corpus_brief_page(
        {"corpus_brief": short_payload},
        _inventory(),
        section="corpus_brief_findings",
        cursor=0,
        limit=20,
        include_evidence_ids=True,
    )
    assert readable_page["items"][0]["statement"] == "A complete short finding."
    assert readable_page["items"][0]["source_ref"] == "S01"
    assert "source_id" not in readable_page["items"][0]


def test_research_job_includes_only_handoff_findings_inside_preferred_source_scope() -> None:
    selected_finding = _finding().model_copy(
        update={
            "question_links": [_finding().question_links[0]],
            "source_relations": [
                FindingSourceRelation(
                    related_source_id="source-2",
                    relationship="updates",
                    rationale="The second source updates this finding.",
                    evidence_ids=["evidence-2"],
                )
            ],
        }
    )
    unrelated_finding = _finding(statement="A different source also mentions the question.").model_copy(
        update={
            "finding_id": "finding-2",
            "source_id": "source-2",
            "source_version_ids": ["version-2"],
            "assessment_id": "assessment-2",
            "evidence_ids": ["evidence-2"],
            "question_links": [
                FindingQuestionLink(
                    question_id="q_mechanism",
                    requirement_ids=["r_actor"],
                )
            ],
        }
    )
    inventory = SourceMap(
        source_cards=[
            *_inventory().source_cards,
            SourceCard(
                source_id="source-2",
                source_version_ids=["version-2"],
                evidence_ids=["evidence-2"],
                title="Source two",
                source_type="web",
                summary="A second source.",
            ),
        ]
    )
    brief = CorpusBrief(
        schema_version="corpus_brief.v2",
        generation_run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        run_corpus_view_id=str(uuid.uuid4()),
        corpus_revision=1,
        attempt=1,
        source_map_artifact_id=str(uuid.uuid4()),
        source_map_hash="hash",
        intent=CorpusBriefIntent(user_prompt="Explain it."),
        coverage_questions=_questions()[:1],
        source_findings=[selected_finding, unrelated_finding],
        coverage_outcomes=[
            CoverageOutcome(
                outcome_id="outcome-mechanism",
                question_id="q_mechanism",
                status="covered",
                finding_ids=["finding-1"],
                requirement_results=[
                    CoverageRequirementResult(
                        requirement_id="r_actor",
                        status="covered",
                        finding_ids=["finding-1"],
                        summary="The actor is known.",
                    ),
                    CoverageRequirementResult(
                        requirement_id="r_mechanism",
                        status="covered",
                        finding_ids=["finding-1"],
                        summary="The mechanism is known.",
                    ),
                ],
                orientation_summary="The selected finding covers the question.",
            )
        ],
        research_handoff=[
            {
                "handoff_id": "handoff-1",
                "objective": "Deep-read the selected finding.",
                "question_ids": ["q_mechanism"],
                "finding_ids": ["finding-1"],
                "preferred_source_ids": ["source-1"],
                "starting_evidence_ids": ["evidence-1"],
            }
        ],
    )

    job = _jobs_from_corpus_brief(brief, inventory, allowed_source_refs=["S01", "S02"])[0]

    assert job["source_refs"] == ["S01"]
    assert [item["finding_id"] for item in job["source_findings"]] == ["finding-1"]
    assert job["source_findings"][0]["source_relations"] == []
    assert job["starting_evidence_ids"] == ["evidence-1"]
    finding_page = _full_corpus_brief_page(
        {"corpus_brief": brief.model_dump(mode="json")},
        inventory,
        section="corpus_brief_findings",
        cursor=0,
        limit=20,
        include_evidence_ids=True,
        job=job,
    )
    assert finding_page["record_count"] == 1
    assert "finding-2" not in str(finding_page)
    assert "evidence-2" not in str(finding_page)

    brief.research_handoff[0].finding_ids.append("finding-2")
    with pytest.raises(ValueError, match="outside the job's preferred source scope"):
        _jobs_from_corpus_brief(brief, inventory, allowed_source_refs=["S01", "S02"])


def test_repair_attempt_carries_and_remaps_durable_finding_lineage() -> None:
    workspace = {
        "source_order": ["S01"],
        "triage_semantic_revision": 7,
        "source_findings": [
            {
                "finding_id": "finding-1",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "statement": "A durable finding.",
                "evidence_ids": ["evidence-old"],
                "question_links": [
                    {"question_id": "q-mechanism", "requirement_ids": ["r-mechanism"]}
                ],
            }
        ],
        "source_assessments": [
            {
                "assessment_id": "assessment-1",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_id": "source-1",
                "finding_ids": ["finding-1"],
                "evidence_ids": ["evidence-old"],
            }
        ],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome-1",
                "set_id": "set-1",
                "question_id": "q-mechanism",
                "finding_ids": ["finding-1"],
                "evidence_ids": ["evidence-old"],
            }
        ],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "mapped",
                "source_refs": ["S01"],
                "question_ids": ["q-mechanism"],
                "evidence_ids": ["evidence-old"],
            }
        ],
        "processed_sets": [],
        "mapped_sources": ["S01"],
        "deferred_sources": [],
    }

    remapped = reconcile_carried_triage_evidence(
        workspace,
        evidence_id_map={"evidence-old": "evidence-new"},
        invalid_evidence_ids=set(),
    )
    reopened = reconcile_carried_triage_evidence(
        workspace,
        evidence_id_map={},
        invalid_evidence_ids={"evidence-old"},
    )

    assert remapped["source_findings"][0]["evidence_ids"] == ["evidence-new"]
    assert remapped["source_assessments"][0]["finding_ids"] == ["finding-1"]
    assert remapped["coverage_outcomes"][0]["finding_ids"] == ["finding-1"]
    assert reopened["source_findings"] == []
    assert reopened["source_assessments"] == []
    assert reopened["coverage_outcomes"] == []
    assert reopened["source_sets"][0]["status"] == "planned"
    assert reopened["source_sets"][0]["repair_source_refs"] == ["S01"]
