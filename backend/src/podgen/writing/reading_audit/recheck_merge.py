"""Materialize a delta judgment without inventing claims or assigning evidence across splits."""

from copy import deepcopy

from podgen.domain.errors import ContractError


def expand(wire, context, prior_decisions):
    def require(value, code):
        if not value:
            raise ContractError(code)

    require(context["phase"] == "recheck", "audit_delta_requires_recheck")
    confirmation = wire.get("retained_confirmation")
    require(
        confirmation and confirmation.get("confirmed") is True and confirmation.get("grounds", "").strip(),
        "audit_delta_confirmation_missing",
    )
    previous = {row["decision_id"]: row for row in prior_decisions}
    exposed = {row["decision_id"]: row for row in context["previous_decisions"]}
    require(set(previous) == set(exposed), "audit_delta_previous_report_mismatch")
    updates = wire["previous_dispositions"]
    keys = [row["previous_decision_id"] for row in updates]
    require(
        len(keys) == len(set(keys)) and set(keys) <= set(previous), "audit_delta_update_reference_invalid"
    )
    changed = {row["previous_decision_id"]: row for row in updates}
    current = wire["audit"]["claim_checks"]
    ids = [row["decision_id"] for row in current]
    require(len(ids) == len(set(ids)), "audit_duplicate_decision")
    introduced = set(ids)
    targets = {key for row in updates for key in row["current_decision_ids"]}
    for row in updates:
        require(row["disposition"] in ("reassessed", "retired"), "audit_delta_retained_exception_invalid")
        require(
            bool(row["current_decision_ids"]) == (row["disposition"] == "reassessed"),
            "audit_delta_update_targets_invalid",
        )
        require(
            len(row["current_decision_ids"]) == len(set(row["current_decision_ids"])),
            "audit_delta_update_targets_invalid",
        )
        require(set(row["current_decision_ids"]) <= introduced, "audit_delta_update_targets_invalid")
    require(not (introduced & (set(previous) - set(changed))), "audit_delta_decision_collision")
    require(not ((introduced & set(previous)) - targets), "audit_delta_reused_id_unlinked")
    retained = []
    dispositions = []
    for key, prior in previous.items():
        if key in changed:
            dispositions.append(deepcopy(changed[key]))
            continue
        item = exposed[key]
        anchors = item["anchors"] if item.get("anchor_version") == "current" else None
        require(not item.get("requires_update") and anchors, "audit_delta_changed_decision_unaddressed")
        require(
            prior["contextual_meaning"] == item["contextual_meaning"]
            and prior["context_scope"] == item["context_scope"]
            and prior["verdict"] == item["verdict"]
            and prior["source_refs"] == item["source_refs"],
            "audit_delta_previous_report_mismatch",
        )
        carried = deepcopy(prior)
        carried["anchors"] = deepcopy(anchors)
        retained.append(carried)
        dispositions.append(
            dict(
                previous_decision_id=key,
                disposition="retained",
                current_decision_ids=[key],
                context_assessment=confirmation["grounds"],
                reason="Confirmed with the unchanged contextual decision and its exact retained evidence.",
            )
        )
    output = deepcopy(wire)
    output.pop("retained_confirmation")
    by_id = {row["decision_id"]: row for row in (*retained, *current)}
    ordered = {}
    for disposition in dispositions:
        for key in disposition["current_decision_ids"]:
            ordered.setdefault(key, by_id[key])
    for row in current:
        ordered.setdefault(row["decision_id"], row)
    output["audit"]["claim_checks"] = deepcopy(list(ordered.values()))
    output["previous_dispositions"] = dispositions
    proof = dict(
        retained_decision_ids=[row["decision_id"] for row in retained],
        updated_decision_ids=keys,
        confirmation=deepcopy(confirmation),
    )
    return output, proof
