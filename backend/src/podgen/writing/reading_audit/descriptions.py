"""Field contents for complete contextual audits; editorial policies live in role instructions."""

from podgen.writing.compact_editorial_descriptions import describe_reader as describe_reader_fields

COMMON = {
    "coverage": "Ordered, non-overlapping inclusive ranges covering every current utterance exactly once, from index 0 through the final utterance without gaps. Each range records its review status, linked decisions and unchecked work.",
    "areas": "Exactly one entry for every role-specific audit area, with status, linked decisions and explanation. A reviewed area must link at least one decision whose verdict is not unchecked and whose claim type or aspect matches that area.",
    "overall_judgment": "Assessment of the complete current account, grounded in its actual content and the recorded decisions.",
}
DECISION = {
    "decision_id": "Unique local identifier for this current audit decision: D followed by a positive integer without leading zeros, such as D1 or D2.",
    "anchors": "Exact current spoken words locating the assessed passage.",
    "contextual_meaning": "What the connected passage conveys to this audience in the surrounding account.",
    "context_scope": "The subject, time, extent, attribution and surrounding context to which this decision applies.",
    "rationale": "Grounds for this decision at its recorded contextual scope.",
    "memory_summary": "Separately authored compact account of this decision for reviewer memory, at most 1600 characters. Full meaning and rationale remain in the complete audit.",
}
RELATIONS = {
    "finding_links": "Links from every current finding index to the current decisions establishing it.",
    "assignment_results": "Execution and effect of each supplied writer assignment; empty on the first audit.",
    "previous_dispositions": "Applicability of every supplied previous own-role decision to the current version; empty on the first audit.",
    "late_findings": "Concrete basis and substantive consequence for every mandatory finding in a subsequent review; empty on the first audit.",
    "effect_on_episode": "What the revision gained, preserved or lost across the episode; null on the first audit, which has no revision to compare.",
}
DEFINITIONS = {
    "CoverageRange": {
        "first_utterance_index": "First current utterance in this inclusive range, indexed from zero.",
        "last_utterance_index": "Last current utterance in this inclusive range, indexed from zero.",
        "status": "Whether this range was reviewed, remains unchecked or contains no applicable material for this role.",
        "decision_ids": "Current audit decisions recording the review of this range; at least one is required when status is reviewed.",
        "explanation": "Work performed, work still unchecked or grounds for inapplicability in this range.",
    },
    "DecisionDisposition": {
        "previous_decision_id": "Exact identifier of the supplied previous own-role decision.",
        "disposition": "Whether that decision remains applicable, was reassessed or no longer applies.",
        "current_decision_ids": "Current decisions carrying forward or reassessing the previous decision; empty when retired.",
        "context_assessment": "Comparison of the previous and current meaning, surrounding context and applicable evidential basis.",
        "reason": "Why this previous decision is retained, reassessed or retired.",
    },
    "LateFinding": {
        "finding_index": "Zero-based index of the current mandatory finding.",
        "reason_kind": "Category of the concrete basis for requiring this repair now.",
        "previous_decision_ids": "Previous own-role decisions relevant to this finding and its reassessment.",
        "previous_coverage_indices": "Zero-based previous coverage entries locating work that was unchecked or omitted.",
        "assignment_ids": "Exact supplied writer assignments whose unresolved problems bear on this finding.",
        "current_decision_ids": "Current decisions demonstrating the problem and its present scope.",
        "grounds": "Actual unresolved work, introduced error, contextual or evidence change, or substantive earlier omission; not a repetition of the category name.",
        "substantive_consequence": "How the demonstrated problem would materially mislead or impair this episode if left unchanged.",
    },
    "FindingLink": {
        "finding_index": "Zero-based index of the current finding.",
        "decision_ids": "Current contextual decisions that establish this finding.",
    },
    "EvidenceRef": {
        "from_review": "Whether the evidence was supplied in this review or retained from the previous own-role audit.",
        "evidence_id": "Exact offered evidence alias: current B evidence or successful E reads, or retained H evidence from previous memory.",
    },
}


def _fields(schema, descriptions):
    for field, description in descriptions.items():
        if field in schema.get("properties", {}):
            schema["properties"][field]["description"] = description


def _describe(schema, *, fact):
    _fields(schema, RELATIONS)
    _fields(
        schema,
        {
            "reading_mode": "Informed assessment with factual evidence and remit supplied."
            if fact
            else "Script-only for the initial cold reading; informed when previous review context is supplied.",
            "listener_answer": "The factual account this episode conveys at its contextual scope. Individual checks are recorded in audit."
            if fact
            else "The understanding listeners develop as the spoken episode unfolds.",
            "important_limits": "Unresolved evidential boundaries affecting the recorded contextual judgments; not a list of spoken qualifications."
            if fact
            else "Limits in the current speech that materially affect this listening experience.",
        },
    )
    defs = schema.get("$defs", {})
    for name, descriptions in DEFINITIONS.items():
        if name in defs:
            _fields(defs[name], descriptions)
    audit = defs.get("FactAudit" if fact else "ReaderAudit", {})
    audit["description"] = (
        "Complete contextual factual assessment of this version, including positive decisions and unchecked work."
        if fact
        else "Complete judgment of this telling, including its cumulative voice, successful passages, defects and unchecked work."
    )
    _fields(audit, COMMON)
    if not fact:
        _fields(
            audit,
            {
                "overall_judgment": "Rationale for the recording decision, connecting passage judgments with the whole telling's development, voice, rhythm and perspective for this audience."
            },
        )
    _fields(
        audit,
        {
            "claim_checks": "Contextual claims actually assessed, with individual factual verdicts and inspected evidence."
        }
        if fact
        else {
            "judgments": "Decisions on actual passages and their contribution to the whole telling, including acceptable work."
        },
    )
    decision = defs.get("ClaimCheck" if fact else "ReaderJudgment", {})
    _fields(decision, DECISION)
    _fields(
        decision,
        {
            "claim_types": "Factual, causal, chronological or generalizing relations assessed by this check.",
            "verdict": "Supported, contradicted, still unsupported after checking, or unchecked at the recorded contextual scope.",
            "source_refs": "Offered evidence or successful source reads establishing this assessment, with current or previous provenance.",
        }
        if fact
        else {
            "aspects": "Aspects of the listening experience assessed by this decision.",
            "verdict": "Whether this passage is acceptable, needs a concrete change or remains unchecked.",
        },
    )
    area = defs.get("FactArea" if fact else "ReaderArea", {})
    _fields(
        area,
        {
            "aspect": "Role-specific area of the complete audit.",
            "status": "Whether this area was reviewed, remains unchecked or does not apply to this account.",
            "decision_ids": "Current decisions recording this area. For reviewed status, at least one linked decision must have a verdict other than unchecked and contain the matching claim type: factual_accuracy maps to factual, causality to causal, chronology to chronological, and generalization to generalization."
            if fact
            else "Current decisions recording this area. For reviewed status, at least one linked decision must have a verdict other than unchecked and include this area's aspect in its aspects list.",
            "explanation": "Assessment performed, remaining unchecked work or grounds for inapplicability.",
        },
    )


def describe_reader(schema):
    describe_reader_fields(schema)
    _describe(schema, fact=False)


def describe_fact(schema):
    _fields(
        schema,
        {
            "findings": "Contextual factual discrepancies and useful optional refinements, linked to the audit decisions establishing them. May be empty.",
            "recommendation": "Decision on remaining required factual repairs or blocking evidence gaps; keep when none remain, including when optional refinements remain.",
            "strengths": "Supported wording and useful factual contributions to retain.",
            "revision_scope": "Editable scale and ranges needed for required factual repairs and their dependent passages; none when no required repair remains.",
        },
    )
    finding = schema.get("$defs", {}).get("SourcedFactFinding", {})
    _fields(
        finding,
        {
            "observation": "Current contextual claim, its demonstrated discrepancy from inspected evidence, and the consequence for factual understanding.",
            "requested_change": "Concrete repair and supported meaning the revised passage should convey, including dependent claims and joins.",
            "priority": "Severity of the demonstrated contextual discrepancy: required important or blocking work, or an optional refinement of an already adequate account.",
            "preserve": "Essential supported meaning, factual conditions and useful contribution to retain. May be empty.",
            "support_labels": "Exact offered card IDs supporting this finding; historical cards without card_id use their offered label keys.",
            "source_read_ids": "Exact successful current E-read aliases or retained H-evidence aliases supporting this finding and its correction; empty when the original cards suffice.",
        },
    )
    _describe(schema, fact=True)
