"""New admission rules, retained proof and frozen compatibility; no DB or providers."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.writing.reading_audit.admissibility import carry_retained
from podgen.writing.reading_audit.consistency import (
    ADMISSIBILITY_VERSION,
    RETAINED_EVIDENCE_VERSION,
    VERSION,
)
from podgen.writing.reading_audit.input import initial_context
from podgen.writing.reading_audit.registry import model, schema
from podgen.writing.reading_audit.validation import validate
from tests.reading_audit_cases import SCRIPT, recheck_context, report
from tests.revision_cases import scope
from tests.schema_cases import without_descriptions


def factual(*, context=None, required=False):
    value = report(fact=True, context=context, required=required)
    for area in value["audit"]["areas"]:
        if area["status"] == "unchecked":
            area.update(status="not_applicable", explanation="No assertion of this kind in this fixture.")
    return value


def check(value, context=None, *, version=ADMISSIBILITY_VERSION, script=SCRIPT):
    parsed = model("fact-review").model_validate(value)
    output = parsed.model_dump(mode="json")
    validate(output, script, context or initial_context(fact=True), fact=True, consistency_contract=version)
    return combine(dict(fact=output), consistency_contract=version)


@pytest.mark.parametrize("gap", ["claim", "range", "area"])
def test_keep_cannot_hide_unchecked_work_in_a_report_or_terminal_outcome(gap):
    value = factual()
    if gap == "claim":
        value["audit"]["claim_checks"][0].update(verdict="unchecked", source_refs=[])
        for area in value["audit"]["areas"]:
            if area["decision_ids"]:
                area.update(status="unchecked")
    elif gap == "range":
        value["audit"]["coverage"][0].update(status="unchecked")
    else:
        next(row for row in value["audit"]["areas"] if row["status"] == "not_applicable").update(
            status="unchecked"
        )
    with pytest.raises(ContractError, match="audit_fact_keep_incomplete"):
        check(value)
    with pytest.raises(ContractError, match="audit_fact_keep_incomplete"):
        combine(dict(fact=value), consistency_contract=ADMISSIBILITY_VERSION)
    assert check(value, version=VERSION)["recommendation"] == "keep"


@pytest.mark.parametrize("verdict", ["contradicted", "unsupported"])
def test_an_asserted_defect_cannot_be_downgraded_to_optional_accuracy(verdict):
    value = factual(required=True)
    value["audit"]["claim_checks"][0]["verdict"] = verdict
    assert check(value)["recommendation"] == "revise"
    value["findings"][0]["priority"] = "optional"
    value.update(recommendation="keep", revision_scope=scope("none"))
    assert check(value, version=VERSION)["recommendation"] == "keep"
    with pytest.raises(ContractError, match="audit_fact_defect_requires_repair"):
        check(value)


def test_supported_optional_refinement_still_allows_keep():
    value = factual(required=True)
    value["audit"]["claim_checks"][0]["verdict"] = "supported"
    value["findings"][0]["priority"] = "optional"
    value.update(recommendation="keep", revision_scope=scope("none"))
    assert check(value)["recommendation"] == "keep"


def test_checked_nearby_names_do_not_discharge_a_separate_unchecked_attribution():
    value = factual()
    checked = value["audit"]["claim_checks"][0]
    checked["contextual_meaning"] = "The names and the count of signs are supported."
    missing = deepcopy(checked)
    missing.update(
        decision_id="D2",
        contextual_meaning="A separate attribution about the alphabetic hypothesis remains unverified.",
        verdict="unchecked",
        source_refs=[],
    )
    value["audit"]["claim_checks"].append(missing)
    value["audit"]["coverage"][0]["decision_ids"].append("D2")
    assert check(value, version=VERSION)["recommendation"] == "keep"
    with pytest.raises(ContractError, match="audit_fact_keep_incomplete"):
        check(value)


def test_removing_an_unverified_incidental_detail_is_a_valid_required_repair():
    value = factual(required=True)
    value["audit"]["claim_checks"][0].update(verdict="unsupported", source_refs=[])
    value["findings"][0].update(
        requested_change="Remove the unsupported incidental name and adjust its dependent reference.",
        source_read_ids=[],
    )
    assert check(value)["recommendation"] == "revise"


def retained():
    context = recheck_context(fact=True)
    context["previous_decisions"][0]["source_refs"] = [dict(from_review="previous", evidence_id="H1")]
    value = factual(context=context)
    value["audit"]["claim_checks"][0]["source_refs"] = [dict(from_review="previous", evidence_id="H1")]
    value["previous_dispositions"][0]["disposition"] = "retained"
    return value, context


def test_recheck_carries_the_accepted_verdict_and_frozen_proof_without_new_reads():
    value, context = retained()
    assert check(value, context)["recommendation"] == "keep"
    assert value["audit"]["claim_checks"][0]["source_refs"] == [
        dict(from_review="previous", evidence_id="H1")
    ]


def test_retained_cannot_mean_a_different_verdict():
    value, context = retained()
    value["audit"]["claim_checks"][0]["verdict"] = "unchecked"
    value["recommendation"] = "needs_material"
    for area in value["audit"]["areas"]:
        area.update(status="unchecked")
    with pytest.raises(ContractError, match="audit_retained_verdict_changed"):
        check(value, context)


def test_retained_cannot_silently_replace_its_evidential_basis():
    value, context = retained()
    value["audit"]["claim_checks"][0]["source_refs"] = [dict(from_review="current", evidence_id="E1")]
    with pytest.raises(ContractError, match="audit_retained_evidence_missing"):
        check(value, context)
    # A fresh assessment can use new evidence; its authenticity is checked by
    # the existing evidence resolver, separately from this relational contract.
    value["previous_dispositions"][0]["disposition"] = "reassessed"
    assert check(value, context)["recommendation"] == "keep"


def carried(value, context):
    result = carry_retained(value, context)
    return result, check(value, context, version=RETAINED_EVIDENCE_VERSION)


def test_archived_v3_carries_frozen_evidence_instead_of_rejecting_its_omission():
    value, context = retained()
    value["audit"]["claim_checks"][0]["source_refs"] = [dict(from_review="current", evidence_id="E1")]
    with pytest.raises(ContractError, match="audit_retained_evidence_missing"):
        check(deepcopy(value), context)
    result, outcome = carried(value, context)
    assert outcome["recommendation"] == "keep"
    assert value["audit"]["claim_checks"][0]["source_refs"] == [
        dict(from_review="current", evidence_id="E1"),
        dict(from_review="previous", evidence_id="H1"),
    ]
    assert result == [
        dict(
            previous_decision_id="D1",
            current_decision_ids=["D1"],
            source_refs=[dict(from_review="previous", evidence_id="H1")],
        )
    ]
    assert carry_retained(value, context) == []


def split(value, successor_id):
    second = deepcopy(value["audit"]["claim_checks"][0])
    second.update(decision_id="D2", source_refs=[dict(from_review="current", evidence_id="E1")])
    value["audit"]["claim_checks"][0].update(
        decision_id=successor_id, source_refs=[dict(from_review="current", evidence_id="E1")]
    )
    value["audit"]["claim_checks"].append(second)
    value["audit"]["coverage"][0]["decision_ids"] = [successor_id, "D2"]
    for area in value["audit"]["areas"]:
        if area["decision_ids"]:
            area["decision_ids"] = [successor_id]
    value["previous_dispositions"][0]["current_decision_ids"] = [successor_id, "D2"]
    return value


def test_split_retained_decision_carries_evidence_to_the_one_keeping_its_id():
    value, context = retained()
    split(value, "D1")
    result, _ = carried(value, context)
    refs = {row["decision_id"]: row["source_refs"] for row in value["audit"]["claim_checks"]}
    assert dict(from_review="previous", evidence_id="H1") in refs["D1"]
    assert dict(from_review="previous", evidence_id="H1") not in refs["D2"]
    assert result[0]["current_decision_ids"] == ["D1"]


def test_split_without_a_continuing_id_carries_evidence_to_every_successor():
    value, context = retained()
    split(value, "D3")
    result, _ = carried(value, context)
    assert all(
        dict(from_review="previous", evidence_id="H1") in row["source_refs"]
        for row in value["audit"]["claim_checks"]
    )
    assert result[0]["current_decision_ids"] == ["D3", "D2"]


@pytest.mark.parametrize("mapping", ["split", "merge"])
def test_new_readmission_rejects_an_ambiguous_evidence_transfer(mapping):
    value, context = retained()
    if mapping == "split":
        split(value, "D3")
        value["audit"]["claim_checks"][1]["source_refs"] = []
    else:
        value["audit"]["claim_checks"][0]["source_refs"] = []
        value["previous_dispositions"].append(
            dict(value["previous_dispositions"][0], previous_decision_id="D2")
        )
        context["previous_decisions"].append(dict(context["previous_decisions"][0], decision_id="D2"))
    with pytest.raises(ContractError, match="audit_ambiguous_retained_evidence"):
        carry_retained(value, context, require_unique_transfer=True)


def test_new_contract_still_rejects_a_changed_retained_verdict():
    value, context = retained()
    value["audit"]["claim_checks"][0]["verdict"] = "unchecked"
    value["recommendation"] = "needs_material"
    for area in value["audit"]["areas"]:
        area.update(status="unchecked")
    carry_retained(value, context)
    with pytest.raises(ContractError, match="audit_retained_verdict_changed"):
        check(value, context, version=RETAINED_EVIDENCE_VERSION)


def test_real_missed_error_can_reopen_with_its_existing_late_finding_basis():
    context = recheck_context(fact=True)
    value = factual(context=context, required=True)
    assert check(value, context)["recommendation"] == "revise"
    value["late_findings"] = []
    with pytest.raises(ContractError, match="audit_late_findings_unaccounted"):
        check(value, context)


@pytest.mark.parametrize(
    "speech,meaning",
    [
        (
            "В этом расследовании впервые удалось проследить путь воды.",
            "The temporal contrast concerns this investigation, not global scientific priority.",
        ),
        (
            "Согласный на письме один и тот же, а гласную каждый раз подставляют по-своему.",
            "The passage compares forms; it does not assert that ancient Egyptian has only one consonant.",
        ),
    ],
)
def test_contextually_supported_decision_is_not_forced_into_a_stronger_claim(speech, meaning):
    # Contract test of bounded admission, not a simulated model quality verdict.
    script = dict(utterances=[dict(speaker="HOST", text=speech)])
    value = report(script, fact=True)
    for area in value["audit"]["areas"]:
        if area["status"] == "unchecked":
            area.update(status="not_applicable")
    value["audit"]["claim_checks"][0].update(contextual_meaning=meaning)
    assert check(value, script=script)["recommendation"] == "keep"


def test_only_the_new_pin_changes_fact_schema_descriptions():
    frozen = schema("fact-review", consistency_contract=VERSION)
    before = deepcopy(frozen)
    current = schema("fact-review", consistency_contract=ADMISSIBILITY_VERSION)
    assert frozen == before
    assert without_descriptions(current) == without_descriptions(frozen)
    assert "no unchecked claims" in current["properties"]["recommendation"]["description"]
    assert "no unchecked claims" not in frozen["properties"]["recommendation"]["description"]
    assert schema("reader-review", consistency_contract=ADMISSIBILITY_VERSION) == schema(
        "reader-review", consistency_contract=VERSION
    )
    # The retained-evidence successor changes admission only, not the model-facing request.
    assert schema("fact-review", consistency_contract=RETAINED_EVIDENCE_VERSION) == current


def test_retained_hieroglyphs_keep_with_explicit_gaps_remains_historical_only():
    from podgen.writing.reading_audit.consistency import validate as consistency

    root = Path(__file__).resolve().parents[2]
    saved = json.loads(
        (root / "evals/reports/2026-11-15-hieroglyphs-script/fact-review-1-attempt-0.json").read_text()
    )["output"]
    assert saved["recommendation"] == "keep"
    assert {
        row["decision_id"] for row in saved["audit"]["claim_checks"] if row["verdict"] == "unchecked"
    } == {"D4", "D7", "D12"}
    consistency(saved, VERSION)
    with pytest.raises(ContractError, match="audit_fact_keep_incomplete"):
        consistency(saved, ADMISSIBILITY_VERSION)
