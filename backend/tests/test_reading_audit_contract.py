"""Coverage and reference checks cannot manufacture checked decisions or contextual truth."""

from copy import deepcopy

import pytest
from podgen.domain.errors import ContractError
from podgen.writing.fact_review import snapshot_model as fact_snapshot
from podgen.writing.reader_schema import snapshot_model as reader_snapshot
from podgen.writing.reading_audit import FACT_SCHEMA, READER_SCHEMA, RECHECK_SCHEMA, VERSION
from podgen.writing.reading_audit.input import initial_context
from podgen.writing.reading_audit.registry import from_manifest, model, schema
from podgen.writing.reading_audit.validation import validate
from podgen.writing.roles import WriteOutput
from pydantic import ValidationError

from tests.reading_audit_cases import SCRIPT, recheck_context, report


@pytest.mark.parametrize("fact", [False, True])
def test_full_first_audit_and_recheck_keep_raw_reason_and_separate_compact_memory(fact):
    for context in (initial_context(fact=fact), recheck_context(fact=fact)):
        value = report(fact=fact, context=context)
        parsed = model("fact-review" if fact else "reader-review").model_validate(value)
        parsed.validate_against(WriteOutput.model_validate(SCRIPT))
        validate(parsed.model_dump(mode="json"), SCRIPT, context, fact=fact)
        decision = value["audit"]["claim_checks" if fact else "judgments"][0]
        assert len(decision["rationale"]) > 1600
        assert parsed.model_dump(mode="json") == value
        assert schema("fact-review" if fact else "reader-review")["additionalProperties"] is False


def test_new_snapshot_is_explicit_even_with_no_findings_and_pin_mix_is_rejected():
    assert reader_snapshot(report()).__name__ == "AuditedReader"
    assert fact_snapshot(report(fact=True)).__name__ == "AuditedFact"
    pin = dict(
        reading_audit_contract=VERSION,
        reader_review_schema_version=READER_SCHEMA,
        fact_review_schema_version=FACT_SCHEMA,
        revision_verification_contract=RECHECK_SCHEMA,
    )
    assert from_manifest(pin) == VERSION and from_manifest({}) is None
    for field in pin:
        bad = deepcopy(pin)
        bad.pop(field)
        with pytest.raises(ContractError):
            from_manifest(bad)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "overlap",
        "out_of_order",
        "unknown_decision",
        "voice_missing",
        "causality_without_causal_decision",
        "effect",
        "too_long_memory",
    ],
)
def test_incomplete_or_contradictory_report_is_not_a_complete_audit(fault):
    fact = fault == "causality_without_causal_decision"
    value = report(fact=fact)
    audit = value["audit"]
    if fault == "missing":
        audit["coverage"][0]["last_utterance_index"] = 0
    elif fault == "overlap":
        audit["coverage"].append(deepcopy(audit["coverage"][0]))
    elif fault == "out_of_order":
        audit["coverage"][0]["first_utterance_index"] = 1
    elif fault == "unknown_decision":
        audit["coverage"][0]["decision_ids"] = ["D99"]
    elif fault == "voice_missing":
        audit["areas"] = [row for row in audit["areas"] if row["aspect"] != "voice"]
    elif fault == "causality_without_causal_decision":
        next(row for row in audit["areas"] if row["aspect"] == "causality").update(
            status="reviewed", decision_ids=["D1"]
        )
    elif fault == "effect":
        value["effect_on_episode"] = "An invented edit on a first pass."
    else:
        audit["judgments"][0]["memory_summary"] = "x" * 1601
    with pytest.raises((ContractError, ValidationError)):
        parsed = model("fact-review" if fact else "reader-review").model_validate(value)
        validate(parsed.model_dump(mode="json"), SCRIPT, initial_context(fact=fact), fact=fact)


def test_empty_findings_does_not_imply_checked_work_and_explicit_gaps_are_retained():
    value = report()
    value["audit"]["coverage"][0].update(
        status="unchecked", decision_ids=[], explanation="Explicitly not processed."
    )
    for row in value["audit"]["areas"]:
        row.update(status="unchecked", decision_ids=[], explanation="Not audited.")
    value["audit"]["judgments"] = []
    parsed = model("reader-review").model_validate(value)
    validate(parsed.model_dump(mode="json"), SCRIPT, initial_context(fact=False), fact=False)
    assert parsed.audit.coverage[0].status == "unchecked" and not parsed.findings


@pytest.mark.parametrize(
    "fault",
    ["lost_disposition", "unlinked_late", "unknown_prior", "gap_outside", "open_optional", "open_keep"],
)
def test_late_findings_and_open_original_assignments_have_bound_actual_basis(fault):
    context = recheck_context(fact=True)
    value = report(
        fact=True, context=context, required=True, still_open=True, source_id="H1", source_review="previous"
    )
    if fault == "lost_disposition":
        value["previous_dispositions"] = []
    elif fault == "unlinked_late":
        value["late_findings"] = []
    elif fault == "unknown_prior":
        value["late_findings"][0]["previous_decision_ids"] = ["D99"]
    elif fault == "gap_outside":
        value["late_findings"][0]["previous_coverage_indices"] = [99]
    elif fault == "open_optional":
        value["findings"][0]["priority"] = "optional"
    else:
        value.update(recommendation="keep")
    with pytest.raises((ContractError, ValidationError)):
        parsed = model("fact-review-1").model_validate(value)
        validate(parsed.model_dump(mode="json"), SCRIPT, context, fact=True)


@pytest.mark.parametrize("fact", [False, True])
def test_audit_anchors_participate_in_validation_and_address_only_repair(fact):
    from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan

    value = report(fact=fact)
    field = "claim_checks" if fact else "judgments"
    value["audit"][field][0]["anchors"][0]["utterance_index"] = 999
    parsed = model("fact-review" if fact else "reader-review").model_validate(value)
    script = WriteOutput.model_validate(SCRIPT)
    with pytest.raises(ValueError):
        parsed.validate_against(script)
    plan = AnchorRepairPlan.build(script, parsed)
    assert len(plan.broken) == 1 and plan.broken[0].group.startswith("audit_")
    repaired = plan.apply(
        AnchorChoices.model_validate(
            dict(choices=[dict(anchor_id=plan.broken[0].anchor_id, selector_id=plan.passages[0].selector_id)])
        )
    )
    expected = deepcopy(value)
    expected["audit"][field][0]["anchors"][0] = dict(utterance_index=0, quote=SCRIPT["utterances"][0]["text"])
    assert repaired.model_dump(mode="json") == expected


@pytest.mark.parametrize(
    "stage",
    ["reader-review", "fact-review", "reader-review-1", "fact-review-1", "reader-review-2", "fact-review-2"],
)
def test_all_audit_schemas_have_provider_supported_reference_nodes(stage):
    from podgen.runtime.evaluation.role_schema import response_schema
    from podgen.writing.revision.verification import bind_assignments

    from tests.test_model_wire_schema import assert_reference_nodes

    native = response_schema(
        stage,
        "editorial-role-packets/1",
        reader_schema_version=READER_SCHEMA,
        fact_schema_version=FACT_SCHEMA,
        revision_verification_contract=RECHECK_SCHEMA,
    )
    assert_reference_nodes(bind_assignments(native, dict(assignments=[dict(id="F1")])))


@pytest.mark.parametrize("stage", ["reader-review", "fact-review"])
def test_actual_first_audit_requests_have_provider_supported_reference_nodes(stage):
    from uuid import uuid4

    from podgen.runtime.evaluation.preview import preview_request

    from tests.source_excerpt_cases import data_and_profile
    from tests.test_model_wire_schema import assert_reference_nodes

    materials, _, _, _, script, _, _, _, profile = data_and_profile()
    body = preview_request(uuid4(), stage, profile(stage), materials, draft=script)
    assert_reference_nodes(body["text"]["format"]["schema"])
