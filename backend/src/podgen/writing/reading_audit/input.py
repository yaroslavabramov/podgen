"""A fact-only index refers to evidence already present in the actual delivered packet."""

from copy import deepcopy


def initial_context(*, fact):
    value = dict(
        phase="first",
        assignments=[],
        previous_decisions=[],
        previous_coverage=[],
        previous_recommendation=None,
        previous_overall_judgment=None,
        revision_operation=None,
        previous_script_in_order=[],
    )
    if fact:
        value["previous_evidence"] = []
    return value


def packet_index(payload):
    result = []
    for field in ("evidence", "context_excerpts"):
        for index, row in enumerate(payload.get(field, [])):
            result.append(
                dict(
                    evidence_id=f"B{len(result) + 1}",
                    path=f"/{field}/{index}",
                    source=row["source"],
                    kind="primary" if field == "evidence" else "context",
                )
            )
    return result


def attach(payload, context, *, fact):
    payload["audit_context"] = deepcopy(context)
    if fact:
        payload["audit_context"]["packet_evidence_index"] = packet_index(payload)
    return payload
