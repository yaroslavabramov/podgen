"""Reference and coverage consistency only; substantive editorial judgments remain model work."""

from podgen.domain.errors import ContractError

READER_ASPECTS = {"story_development", "clarity", "transitions", "repetition", "ending", "voice", "idiom"}
FACT_ASPECTS = {"factual_accuracy", "causality", "chronology", "generalization"}


def _require(value, code):
    if not value:
        raise ContractError(code)


def decisions(output):
    return output["audit"].get("claim_checks", output["audit"].get("judgments", []))


def validate(output, script, context, *, fact, consistency_contract=None):
    from .consistency import validate as validate_consistency

    validate_consistency(output, consistency_contract)
    _require(
        (output["effect_on_episode"] is None) == (context["phase"] == "first"), "audit_effect_phase_mismatch"
    )
    current = decisions(output)
    ids = [row["decision_id"] for row in current]
    _require(len(ids) == len(set(ids)), "audit_duplicate_decision")
    valid = set(ids)
    areas = output["audit"]["areas"]
    aspects = [row["aspect"] for row in areas]
    _require(
        set(aspects) == (FACT_ASPECTS if fact else READER_ASPECTS) and len(aspects) == len(set(aspects)),
        "audit_areas_incomplete",
    )
    next_index = 0
    for row in output["audit"]["coverage"]:
        first, last = row["first_utterance_index"], row["last_utterance_index"]
        _require(
            first == next_index and first <= last < len(script["utterances"]), "audit_coverage_range_invalid"
        )
        next_index = last + 1
    _require(next_index == len(script["utterances"]), "audit_coverage_incomplete")
    for row in [*areas, *output["audit"]["coverage"]]:
        _require(set(row["decision_ids"]) <= valid, "audit_unknown_decision")
        _require(row["status"] != "reviewed" or row["decision_ids"], "audit_reviewed_without_decision")
    by_id = {row["decision_id"]: row for row in current}
    types = dict(
        factual_accuracy="factual",
        causality="causal",
        chronology="chronological",
        generalization="generalization",
    )
    for area in areas:
        if area["status"] != "reviewed":
            continue
        expected = types[area["aspect"]] if fact else area["aspect"]
        _require(
            any(
                by_id[key]["verdict"] != "unchecked"
                and expected in by_id[key]["claim_types" if fact else "aspects"]
                for key in area["decision_ids"]
            ),
            "audit_area_decision_kind_mismatch",
        )
    linked = [row["finding_index"] for row in output["finding_links"]]
    _require(sorted(linked) == list(range(len(output["findings"]))), "audit_findings_unlinked")
    for row in output["finding_links"]:
        _require(set(row["decision_ids"]) <= valid, "audit_unknown_decision")
    assignments = {row["id"] for row in context["assignments"]}
    results = output["assignment_results"]
    _require(
        sorted(row["assignment_id"] for row in results) == sorted(assignments), "audit_assignments_incomplete"
    )
    for row in results:
        index = row["followup_finding"]
        _require((row["outcome"] == "still_open") == (index is not None), "audit_assignment_followup_invalid")
        if index is not None:
            _require(index < len(output["findings"]), "audit_assignment_followup_invalid")
            _require(
                output["recommendation"] != "keep"
                and output["findings"][index]["priority"] in ("important", "blocking"),
                "audit_open_assignment_optional",
            )
    previous = {row["decision_id"] for row in context.get("previous_decisions", [])}
    dispositions = output["previous_dispositions"]
    _require(
        sorted(row["previous_decision_id"] for row in dispositions) == sorted(previous),
        "audit_dispositions_incomplete",
    )
    for row in dispositions:
        _require(set(row["current_decision_ids"]) <= valid, "audit_unknown_decision")
        _require(
            row["disposition"] == "retired" or row["current_decision_ids"],
            "audit_disposition_missing_current",
        )
    mandatory = {
        i for i, row in enumerate(output["findings"]) if row["priority"] in ("blocking", "important")
    }
    late = output["late_findings"]
    _require(
        sorted(row["finding_index"] for row in late)
        == sorted(mandatory if context["phase"] == "recheck" else []),
        "audit_late_findings_unaccounted",
    )
    for row in late:
        _require(
            set(row["current_decision_ids"]) <= valid
            and set(row["previous_decision_ids"]) <= previous
            and set(row["assignment_ids"]) <= assignments,
            "audit_late_reference_invalid",
        )
        gaps = row["previous_coverage_indices"]
        _require(
            len(gaps) == len(set(gaps))
            and all(0 <= index < len(context.get("previous_coverage", [])) for index in gaps),
            "audit_late_coverage_invalid",
        )
        kind = row["reason_kind"]
        if kind == "unresolved_assignment":
            _require(
                row["assignment_ids"]
                and any(
                    result["assignment_id"] in row["assignment_ids"]
                    and result["outcome"] == "still_open"
                    and result["followup_finding"] == row["finding_index"]
                    for result in results
                ),
                "audit_late_assignment_missing",
            )
        else:
            _require(
                row["previous_decision_ids"] or gaps or kind == "introduced_or_regression",
                "audit_late_prior_basis_missing",
            )
            if kind == "introduced_or_regression":
                _require(context.get("revision_operation") is not None, "audit_late_revision_missing")
    if fact:
        for row in current:
            _require(
                row["verdict"] not in ("supported", "contradicted") or row["source_refs"],
                "audit_verdict_evidence_missing",
            )
        from .consistency import ADMISSIBILITY_VERSION

        if consistency_contract == ADMISSIBILITY_VERSION:
            from .admissibility import validate_retained

            validate_retained(output, context)
