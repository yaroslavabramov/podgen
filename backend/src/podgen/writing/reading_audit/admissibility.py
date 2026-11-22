"""Consistency of factual admission, not server-side interpretation of the speech.

decision-findings/2 and /3 apply these rules. The reviewer owns the contextual
claim and evidential judgment; archived reports keep their original semantics.
"""

from collections import Counter
from copy import deepcopy

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


def carry_retained(output, context, *, require_unique_transfer=False):
    """decision-findings/3: the server, not the reviewer, carries a retained decision's frozen evidence.

    Missing previous references join the successor that keeps the previous ID; without one,
    every successor retains the previous verdict and receives them. Unknown IDs are left to
    validation. New local readmission grants allow automatic transfer only
    between a single predecessor and a single successor.
    """
    previous = {row["decision_id"]: row for row in context.get("previous_decisions", [])}
    current = {row["decision_id"]: row for row in output["audit"]["claim_checks"]}
    predecessors = (
        Counter(
            key
            for disposition in output["previous_dispositions"]
            for key in disposition["current_decision_ids"]
        )
        if require_unique_transfer
        else None
    )
    carried = []
    for disposition in output["previous_dispositions"]:
        prior = previous.get(disposition["previous_decision_id"])
        successors = [key for key in disposition["current_decision_ids"] if key in current]
        if disposition["disposition"] != "retained" or prior is None or not successors:
            continue
        cited = {
            (ref["from_review"], ref["evidence_id"])
            for key in successors
            for ref in current[key]["source_refs"]
        }
        missing = [
            ref for ref in prior["source_refs"] if (ref["from_review"], ref["evidence_id"]) not in cited
        ]
        if not missing:
            continue
        if predecessors is not None and (len(successors) != 1 or predecessors[successors[0]] != 1):
            raise ContractError("audit_ambiguous_retained_evidence")
        targets = [prior["decision_id"]] if prior["decision_id"] in successors else successors
        for key in targets:
            current[key]["source_refs"].extend(deepcopy(missing))
        carried.append(
            dict(
                previous_decision_id=prior["decision_id"],
                current_decision_ids=targets,
                source_refs=deepcopy(missing),
            )
        )
    return carried


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
