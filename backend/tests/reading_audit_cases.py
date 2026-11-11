"""Small contextual audit fixtures, not evidence of editorial quality."""

from copy import deepcopy

from podgen.writing.reading_audit.input import initial_context
from podgen.writing.reading_audit.validation import FACT_ASPECTS, READER_ASPECTS

from tests.revision_cases import scope

SCRIPT = dict(
    utterances=[
        dict(speaker="HOST", text="The investigation followed the water."),
        dict(speaker="HOST", text="The story ends within this investigation."),
    ]
)


def report(
    script=SCRIPT,
    *,
    fact=False,
    context=None,
    required=False,
    source_id="E1",
    source_review="current",
    still_open=False,
):
    context = context or initial_context(fact=fact)
    first = context["phase"] == "first"
    anchor = dict(utterance_index=0, quote=script["utterances"][0]["text"])
    check = dict(
        decision_id="D1",
        anchors=[anchor],
        contextual_meaning="The conclusion concerns this investigation.",
        context_scope="The account's established subject and chronology.",
        rationale="FULL_PERSISTED_REASON: "
        + "The evidence and the ordinary listening context are considered together. " * 28,
        memory_summary="This account concerns the investigation; its scope is not a global priority claim.",
    )
    if fact:
        check.update(
            claim_types=["factual"],
            verdict="contradicted" if required else "supported",
            source_refs=[dict(from_review=source_review, evidence_id=source_id)],
        )
    else:
        check.update(aspects=sorted(READER_ASPECTS), verdict="needs_change" if required else "acceptable")
    areas = [
        dict(
            aspect=aspect,
            status="reviewed" if not fact or aspect == "factual_accuracy" else "unchecked",
            decision_ids=["D1"] if not fact or aspect == "factual_accuracy" else [],
            explanation="A recorded contextual judgment."
            if not fact or aspect == "factual_accuracy"
            else "This type of relation has not been audited in this fixture.",
        )
        for aspect in sorted(FACT_ASPECTS if fact else READER_ASPECTS)
    ]
    finding = dict(
        dimension="factuality" if fact else "oral_style",
        priority="important",
        anchors=[anchor],
        observation="The current contextual statement has an evidenced problem.",
        requested_change="Repair the addressed statement without changing its valid context.",
        preserve=["The established subject and tone."],
    )
    if fact:
        finding.update(support_labels=[], source_read_ids=[source_id])
    assignments = [
        dict(
            assignment_id=row["id"],
            outcome="still_open" if still_open else "solved_differently",
            judgment="The actual change was compared with this assignment.",
            followup_finding=0 if still_open else None,
        )
        for row in context["assignments"]
    ]
    late = []
    if not first and required:
        late = [
            dict(
                finding_index=0,
                reason_kind="unresolved_assignment" if still_open else "substantive_missed_issue",
                previous_decision_ids=[row["decision_id"] for row in context["previous_decisions"]],
                previous_coverage_indices=[] if still_open else [0],
                assignment_ids=[row["id"] for row in context["assignments"]],
                current_decision_ids=["D1"],
                grounds="The cited previous assessment does not settle this concrete issue.",
                substantive_consequence="The stated contextual meaning remains materially unsupported.",
            )
        ]
    return dict(
        reading_mode="informed" if fact or not first else "script_only",
        listener_answer="The listener understands the investigation in its stated scope.",
        important_limits=[],
        strengths=[],
        findings=[finding] if required else [],
        research_requests=[],
        duration_note="No timed reading.",
        recommendation="revise" if required else "keep",
        revision_scope=scope("local" if required else "none", anchors=[anchor] if required else []),
        audit=dict(
            coverage=[
                dict(
                    first_utterance_index=0,
                    last_utterance_index=len(script["utterances"]) - 1,
                    status="reviewed",
                    decision_ids=["D1"],
                    explanation="These utterances were assessed as a connected passage.",
                )
            ],
            areas=areas,
            overall_judgment="A contextual judgment of the full supplied account.",
            **{"claim_checks" if fact else "judgments": [check]},
        ),
        finding_links=[dict(finding_index=0, decision_ids=["D1"])] if required else [],
        assignment_results=assignments,
        previous_dispositions=[
            dict(
                previous_decision_id=row["decision_id"],
                disposition="reassessed",
                current_decision_ids=["D1"],
                context_assessment="The changed text, surrounding meaning and available evidence were considered.",
                reason="The current decision explicitly records whether this old conclusion remains applicable.",
            )
            for row in context["previous_decisions"]
        ],
        late_findings=late,
        effect_on_episode=None if first else "The edit's effect on the whole account was assessed.",
    )


def recheck_context(*, fact=False):
    previous = report(fact=fact)["audit"]["claim_checks" if fact else "judgments"]
    value = initial_context(fact=fact)
    value.update(
        phase="recheck",
        assignments=[
            dict(
                id="F1" if fact else "R1",
                origin="fact" if fact else "reader",
                anchors=[dict(utterance_index=0, quote=SCRIPT["utterances"][0]["text"])],
                problem="A concrete commissioned repair.",
                action="Repair it.",
            )
        ],
        previous_decisions=deepcopy(previous),
        previous_coverage=[
            dict(
                first_utterance_index=0,
                last_utterance_index=1,
                status="unchecked",
                decision_ids=[],
                explanation="Explicitly not checked.",
            )
        ],
        revision_operation=dict(script_version=0),
        previous_recommendation="revise",
    )
    return value
