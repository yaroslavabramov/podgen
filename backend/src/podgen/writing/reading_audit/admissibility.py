"""Consistency of factual admission, not server-side interpretation of the speech.

Only decision-findings/2 applies these rules. The reviewer owns the contextual
claim and evidential judgment; archived reports keep their original semantics.
"""

from podgen.domain.errors import ContractError


def validate(output):
    audit = output["audit"]
    required = {
        key
        for link in output["finding_links"]
        if output["findings"][link["finding_index"]]["priority"] in ("important", "blocking")
        for key in link["decision_ids"]
    }
    unresolved = {
        row["decision_id"]
        for row in audit["claim_checks"]
        if row["verdict"] in ("contradicted", "unsupported")
    }
    if not unresolved <= required:
        raise ContractError("audit_fact_defect_requires_repair")
    if output["recommendation"] == "keep":
        if unresolved or any(row["verdict"] == "unchecked" for row in audit["claim_checks"]):
            raise ContractError("audit_fact_keep_incomplete")
        if any(row["status"] == "unchecked" for row in (*audit["coverage"], *audit["areas"])):
            raise ContractError("audit_fact_keep_incomplete")


def validate_retained(output, context):
    """Retaining a decision carries its verdict and bound evidence, not just its ID."""
    previous = {row["decision_id"]: row for row in context.get("previous_decisions", [])}
    current = {row["decision_id"]: row for row in output["audit"]["claim_checks"]}
    for disposition in output["previous_dispositions"]:
        if disposition["disposition"] != "retained":
            continue
        prior = previous[disposition["previous_decision_id"]]
        successors = [current[key] for key in disposition["current_decision_ids"]]
        if any(row["verdict"] != prior["verdict"] for row in successors):
            raise ContractError("audit_retained_verdict_changed")
        before = {(ref["from_review"], ref["evidence_id"]) for ref in prior["source_refs"]}
        after = {(ref["from_review"], ref["evidence_id"]) for row in successors for ref in row["source_refs"]}
        if not before <= after:
            raise ContractError("audit_retained_evidence_missing")


def describe(schema):
    if "FactAudit" not in schema.get("$defs", {}):
        return
    props, definitions = schema["properties"], schema["$defs"]
    props["finding_links"]["description"] = (
        "Link every finding to its current decisions. Every contradicted or unsupported factual"
        " decision needs an important or blocking repair, not optional polish. A required fact"
        " may be corrected, narrowed or removed; an unchecked claim cannot accompany keep."
    )
    props["recommendation"]["description"] = (
        "Keep only when the factual account has no unchecked claims, ranges or applicable areas"
        " and no required defects. Revise to correct or remove an unsupported asserted detail;"
        " needs_material only when a specific evidence gap prevents an adequate bounded account."
        " Optional refinements of supported wording may accompany keep."
    )
    definitions["ClaimCheck"]["properties"]["contextual_meaning"]["description"] += (
        " Assess the proposition actually conveyed in its connected speech, not a stronger"
        " imagined literal reading. Include concrete asserted details and dependent conclusions."
    )
    definitions["ClaimCheck"]["properties"]["verdict"]["description"] += (
        " Unchecked is unfinished work and blocks keep; it does not mean false. A contextual"
        " interpretation may be supported by inspected premises without a verbatim source."
    )
    definitions["DecisionDisposition"]["properties"]["disposition"]["description"] += (
        " Retained carries forward the applicable contextual verdict and its frozen evidence."
        " A changed verdict or evidential basis requires reassessment and concrete grounds."
    )
    definitions["LateFinding"]["properties"]["grounds"]["description"] += (
        " For reopening, identify the changed words, dependent conclusion, new evidence or"
        " specific missed contextual error; another round or a stronger imagined claim is not a basis."
    )
