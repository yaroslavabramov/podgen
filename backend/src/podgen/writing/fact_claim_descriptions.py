"""A contextual claim, its evidence and material consequence precede a fact-check decision."""


def describe_fact_claims(schema):
    props = schema["properties"]
    props["listener_answer"]["description"] = (
        "Identify the consequential, checkable claims conveyed by this script in context, including "
        "proper names, quantities, chronology and causal statements. Use the meaning a listener "
        "would reasonably understand from the surrounding account, rather than its strongest "
        "conceivable literal reading. This is a claim assessment, not a required coverage checklist."
    )
    props["findings"]["description"] = (
        "Report a concrete claim in context, the evidence or specific absence of evidence, and why "
        "the difference matters before choosing severity and repair. Distinguish contradiction, "
        "insufficient confirmation and a materially misleading ambiguity. Research cautions alone "
        "do not establish a defect. May be empty when the account needs no correction."
    )
    props["important_limits"]["description"] = (
        "Only unresolved evidence boundaries that materially affect the assessed claims. This is "
        "not a preliminary audit or a list of qualifications the narrator must voice. May be empty."
    )
    props["recommendation"]["description"] = (
        "Decision following the contextual claim assessments and their material consequences. "
        "Require correction for an established material error or an unsupported detail that this "
        "account needs resolved. Missing confirmation does not establish falsehood. Keep is valid "
        "when no required repair remains; optional refinements may remain."
    )
    finding = schema["$defs"]["FactFinding"]["properties"]
    finding["observation"]["description"] = (
        "State the exact checkable claim conveyed here, compare it with the cited evidence or "
        "identify the specific missing support, then explain the consequence for this account. "
        "Use surrounding speech to establish referents, scope and ordinary meaning. Check a "
        "consequential proper name or number against available source and bibliographic evidence."
    )
    finding["priority"]["description"] = (
        "Choose severity after establishing the evidence difference and its material effect. "
        "Important or blocking requires a necessary repair, not merely a possible stronger "
        "reading, an imaginable alternative or a research caution. An equivalent supported "
        "refinement is optional; omit a finding with no concrete beneficial change."
    )
    finding["requested_change"]["description"] = (
        "The concrete supported meaning the repair must achieve. A dispensable claim can be "
        "removed; a necessary claim may need correction or verification. Do not invent a "
        "replacement fact from missing evidence or require retaining a disputed phrase with an "
        "added warning. Choose one actionable repair supported by the available material."
        " Do not require a new name or detail that the writer cannot verify in their supplied "
        "support; removing an unnecessary full name or retaining a supported surname can suffice."
    )
    finding_order = [
        "anchors",
        "observation",
        "support_labels",
        "priority",
        "requested_change",
        "preserve",
        "dimension",
    ]
    schema["$defs"]["FactFinding"]["properties"] = {key: finding[key] for key in finding_order}
    schema["$defs"]["FactFinding"]["required"] = finding_order
    order = [
        "reading_mode",
        "listener_answer",
        "findings",
        "research_requests",
        "important_limits",
        "strengths",
        "duration_note",
        "revision_scope",
        "recommendation",
    ]
    schema["properties"] = {key: props[key] for key in order}
    schema["required"] = order
