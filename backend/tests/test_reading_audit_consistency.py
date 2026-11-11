"""Negative decisions cannot disappear; optional findings and explicit gaps remain editorial choices."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.reading_audit.source import prepare
from podgen.runtime.evaluation.reading_cycle import combine
from podgen.writing.reading_audit import FACT_SCHEMA, READER_SCHEMA, RECHECK_SCHEMA
from podgen.writing.reading_audit import VERSION as AUDIT_VERSION
from podgen.writing.reading_audit.consistency import KEY, VERSION, from_manifest
from podgen.writing.reading_audit.input import initial_context
from podgen.writing.reading_audit.registry import from_manifest as audit_from_manifest
from podgen.writing.reading_audit.registry import model
from podgen.writing.reading_audit.validation import validate

from tests.reading_audit_cases import SCRIPT, report
from tests.revision_cases import scope


def contradictory(fact, verdict=None):
    value = report(fact=fact, required=True)
    if verdict is not None:
        value["audit"]["claim_checks" if fact else "judgments"][0]["verdict"] = verdict
    value.update(findings=[], finding_links=[], recommendation="keep", revision_scope=scope("none"))
    return value


@pytest.mark.parametrize(
    "fact,verdict", [(False, "needs_change"), (True, "contradicted"), (True, "unsupported")]
)
def test_negative_keep_without_finding_fails_validation_and_final_assembly(fact, verdict):
    value = (
        model("fact-review" if fact else "reader-review")
        .model_validate(contradictory(fact, verdict))
        .model_dump(mode="json")
    )
    with pytest.raises(ContractError, match="audit_negative_decision_unlinked"):
        validate(value, SCRIPT, initial_context(fact=fact), fact=fact, consistency_contract=VERSION)
    readings = {"fact" if fact else "reader": value, "reader" if fact else "fact": report(fact=not fact)}
    with pytest.raises(ContractError, match="audit_negative_decision_unlinked"):
        combine(readings, consistency_contract=VERSION)
    # Frozen pre-fix reports retain their former validation and aggregation semantics.
    validate(value, SCRIPT, initial_context(fact=fact), fact=fact)
    assert combine(readings)["recommendation"] == "keep"


@pytest.mark.parametrize(
    "fact,verdict", [(False, "needs_change"), (True, "contradicted"), (True, "unsupported")]
)
@pytest.mark.parametrize("priority", ["optional", "important", "blocking"])
def test_linked_negative_leaves_materiality_to_the_reviewer(fact, verdict, priority):
    value = report(fact=fact, required=True)
    value["audit"]["claim_checks" if fact else "judgments"][0]["verdict"] = verdict
    value["findings"][0]["priority"] = priority
    if priority == "optional":
        value.update(recommendation="keep", revision_scope=scope("none"))
    parsed = model("fact-review" if fact else "reader-review").model_validate(value)
    validate(
        parsed.model_dump(mode="json"),
        SCRIPT,
        initial_context(fact=fact),
        fact=fact,
        consistency_contract=VERSION,
    )
    assert combine(dict(review=value), consistency_contract=VERSION)["recommendation"] == (
        "keep" if priority == "optional" else "revise"
    )


def test_another_linked_decision_does_not_discharge_the_negative_one():
    value = report(required=True)
    positive = deepcopy(value["audit"]["judgments"][0])
    positive.update(decision_id="D2", verdict="acceptable")
    value["audit"]["judgments"].append(positive)
    value["finding_links"][0]["decision_ids"] = ["D2"]
    with pytest.raises(ContractError, match="audit_negative_decision_unlinked"):
        combine(dict(reader=value), consistency_contract=VERSION)


def test_unchecked_evidence_does_not_automatically_require_a_finding():
    verdict = "unchecked"
    value = report(fact=True)
    value["audit"]["claim_checks"][0].update(verdict=verdict, source_refs=[])
    if verdict == "unchecked":
        value["audit"]["areas"][0].update(status="unchecked")
        for area in value["audit"]["areas"]:
            area.update(status="unchecked", decision_ids=[])
    validate(value, SCRIPT, initial_context(fact=True), fact=True, consistency_contract=VERSION)
    assert combine(dict(fact=value), consistency_contract=VERSION)["recommendation"] == "keep"


def test_pin_requires_an_audit_and_rejects_unknown_version():
    assert from_manifest({}) is None
    for manifest in ({KEY: VERSION}, {KEY: "unknown", "reading_audit_contract": AUDIT_VERSION}):
        with pytest.raises(ContractError, match="unsupported_reading_audit_consistency"):
            audit_from_manifest(manifest)
    with pytest.raises(ContractError, match="audit_consistency_report_missing"):
        combine(
            dict(reader=dict(recommendation="keep", findings=[], research_requests=[])),
            consistency_contract=VERSION,
        )


def test_memory_binding_only_changes_for_explicit_new_pin():
    manifest = dict(
        reading_audit_contract=AUDIT_VERSION,
        reader_review_schema_version=READER_SCHEMA,
        fact_review_schema_version=FACT_SCHEMA,
        revision_verification_contract=RECHECK_SCHEMA,
        materials_hash="frozen",
    )
    args = (SimpleNamespace(), uuid4(), "reader-review")
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4()).model_dump(mode="json")
    legacy = prepare(*args, manifest, SimpleNamespace(), SCRIPT, ref)
    current = prepare(*args, dict(manifest, **{KEY: VERSION}), SimpleNamespace(), SCRIPT, ref)
    assert "consistency_contract" not in legacy.binding
    assert current.binding["consistency_contract"] == VERSION
    assert legacy.view == current.view


@pytest.mark.parametrize("stage", ["reader-review", "fact-review", "reader-review-1", "fact-review-2"])
def test_only_new_pin_exposes_bidirectional_rule_to_the_model(stage):
    from podgen.runtime.evaluation.role_schema import response_schema

    from tests.schema_cases import without_descriptions
    from tests.test_model_wire_schema import assert_reference_nodes

    options = dict(
        reader_schema_version=READER_SCHEMA,
        fact_schema_version=FACT_SCHEMA,
        revision_verification_contract=RECHECK_SCHEMA,
    )
    old = response_schema(stage, "editorial-role-packets/1", **options)
    new = response_schema(
        stage, "editorial-role-packets/1", reading_audit_consistency_contract=VERSION, **options
    )
    text = new["properties"]["finding_links"]["description"]
    assert "Every current reader needs_change or fact contradicted or unsupported" in text
    assert "optional may accompany keep" in text
    assert "Every current reader needs_change" not in old["properties"]["finding_links"]["description"]
    assert without_descriptions(new) == without_descriptions(old)
    assert_reference_nodes(new)
