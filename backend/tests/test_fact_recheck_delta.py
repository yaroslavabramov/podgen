"""Delta reviews retain full reports without silently inheriting evidence across new claims."""

from copy import deepcopy

import pytest

from podgen.domain.errors import ContractError
from podgen.writing.reading_audit.consistency import DELTA_VERSION
from podgen.writing.reading_audit.recheck_changes import attach, comparison
from podgen.writing.reading_audit.recheck_merge import expand
from podgen.writing.reading_audit.registry import model, schema
from podgen.writing.reading_audit.schemas import AuditedFact
from podgen.writing.reading_audit.validation import validate
from tests.reading_audit_cases import SCRIPT, recheck_context, report


def setup(before=SCRIPT, after=SCRIPT):
    context = recheck_context(fact=True)
    prior = deepcopy(context["previous_decisions"])
    attach(context, before, after)
    value = report(after, fact=True, context=context)
    value.update(
        retained_confirmation=dict(
            confirmed=True, grounds="The unchanged contextual conclusions and their evidence still apply."
        ),
        previous_dispositions=[],
    )
    value["audit"]["claim_checks"] = []
    for area in value["audit"]["areas"]:
        if area["status"] == "unchecked":
            area.update(status="not_applicable")
    return context, prior, value


def admitted(value, context, prior, script=SCRIPT):
    parsed = model("fact-review-1", consistency_contract=DELTA_VERSION).model_validate(value)
    parsed.validate_against(script_model(script))
    complete, proof = expand(parsed.model_dump(mode="json"), context, prior)
    parsed = AuditedFact.model_validate(complete)
    parsed.validate_against(script_model(script))
    validate(parsed.model_dump(mode="json"), script, context, fact=True, consistency_contract=DELTA_VERSION)
    return parsed.model_dump(mode="json"), proof


def script_model(value):
    from podgen.writing.roles import WriteOutput

    return WriteOutput.model_validate(value)


def update(previous="D1", targets=("D1",), disposition="reassessed"):
    return dict(
        previous_decision_id=previous,
        disposition=disposition,
        current_decision_ids=list(targets),
        context_assessment="The changed claim and its current context were inspected.",
        reason="A concrete change requires this new contextual judgment.",
    )


def test_single_confirmation_keeps_complete_original_decision_and_refs():
    context, prior, value = setup()
    original = deepcopy(prior)
    complete, proof = admitted(value, context, prior)
    assert complete["audit"]["claim_checks"] == original
    assert complete["previous_dispositions"][0]["disposition"] == "retained"
    assert proof["retained_decision_ids"] == ["D1"] and prior == original


def test_inserted_turn_remaps_anchors_and_shows_exact_change():
    after = deepcopy(SCRIPT)
    after["utterances"].insert(0, dict(speaker="HOST", text="A new opening."))
    context, prior, value = setup(after=after)
    assert context["script_changes"] == [
        dict(kind="insert", before=[], after=[dict(index=0, **after["utterances"][0])])
    ]
    assert "previous_script_in_order" not in context
    complete, _ = admitted(value, context, prior, after)
    assert complete["audit"]["claim_checks"][0]["anchors"][0]["utterance_index"] == 1


@pytest.mark.parametrize("change", ["changed", "deleted", "duplicate"])
def test_edited_deleted_or_ambiguous_anchor_cannot_be_confirmed_implicitly(change):
    after = deepcopy(SCRIPT)
    if change == "changed":
        after["utterances"][0]["text"] = "A changed fact."
    elif change == "deleted":
        after["utterances"].pop(0)
    else:
        after["utterances"].append(deepcopy(after["utterances"][0]))
    context, prior, value = setup(after=after)
    assert context["previous_decisions"][0]["requires_update"]
    with pytest.raises(ContractError, match="audit_delta_changed_decision_unaddressed"):
        admitted(value, context, prior, after)


def test_split_requires_explicit_evidence_for_each_new_claim():
    context, prior, value = setup()
    value["previous_dispositions"] = [update(targets=("D1", "D2"))]
    one = deepcopy(prior[0])
    two = deepcopy(one)
    one["source_refs"] = [dict(from_review="previous", evidence_id="H1")]
    two.update(decision_id="D2", contextual_meaning="A different proposition.", source_refs=[])
    value["audit"]["claim_checks"] = [one, two]
    with pytest.raises(ContractError, match="audit_verdict_evidence_missing"):
        admitted(value, context, prior)
    assert two["source_refs"] == []
    two["source_refs"] = [dict(from_review="current", evidence_id="E2")]
    complete, _ = admitted(value, context, prior)
    assert complete["audit"]["claim_checks"][1]["source_refs"] == two["source_refs"]
    assert all(
        dict(from_review="previous", evidence_id="H1") not in row["source_refs"]
        for row in complete["audit"]["claim_checks"][1:]
    )


def test_merged_claim_uses_only_explicitly_chosen_evidence():
    context, prior, value = setup()
    second = deepcopy(prior[0])
    second.update(decision_id="D2", source_refs=[dict(from_review="previous", evidence_id="H2")])
    prior.append(second)
    context["previous_decisions"].append(deepcopy(second))
    attach(context, SCRIPT, SCRIPT)
    value["previous_dispositions"] = [update("D1"), update("D2")]
    value["audit"]["claim_checks"] = [deepcopy(prior[0])]
    complete, _ = admitted(value, context, prior)
    assert complete["audit"]["claim_checks"][0]["source_refs"] == prior[0]["source_refs"]
    assert (
        dict(from_review="previous", evidence_id="H2")
        not in complete["audit"]["claim_checks"][0]["source_refs"]
    )


def test_retired_claim_needs_explicit_disposition_and_current_coverage():
    context, prior, value = setup()
    value["previous_dispositions"] = [update(targets=(), disposition="retired")]
    value["audit"]["claim_checks"] = [dict(deepcopy(prior[0]), decision_id="D2")]
    for row in [*value["audit"]["coverage"], *value["audit"]["areas"]]:
        row["decision_ids"] = ["D2"] if row["decision_ids"] else []
    complete, _ = admitted(value, context, prior)
    assert [row["decision_id"] for row in complete["audit"]["claim_checks"]] == ["D2"]


@pytest.mark.parametrize(
    "failure",
    ["confirmation", "collision", "unknown_update", "unlinked_id", "retired_targets", "unknown_anchor"],
)
def test_invalid_delta_is_rejected(failure):
    context, prior, value = setup()
    if failure == "confirmation":
        value.pop("retained_confirmation")
    elif failure == "collision":
        value["audit"]["claim_checks"] = [deepcopy(prior[0])]
    elif failure == "unknown_update":
        value["previous_dispositions"] = [update("D9")]
    elif failure == "unlinked_id":
        value["previous_dispositions"] = [update(targets=("D2",))]
        value["audit"]["claim_checks"] = [deepcopy(prior[0]), dict(deepcopy(prior[0]), decision_id="D2")]
    elif failure == "retired_targets":
        value["previous_dispositions"] = [update(disposition="retired")]
    else:
        value["previous_dispositions"] = [update()]
        value["audit"]["claim_checks"] = [deepcopy(prior[0])]
        value["audit"]["claim_checks"][0]["anchors"][0]["utterance_index"] = 42
    with pytest.raises((ValueError, ContractError)):
        admitted(value, context, prior)


def test_new_wire_is_fact_recheck_only_and_legacy_schemas_stay_equal():
    from podgen.writing.reading_audit.consistency import ADMISSIBILITY_VERSION, RETAINED_EVIDENCE_VERSION

    for version in (ADMISSIBILITY_VERSION, RETAINED_EVIDENCE_VERSION):
        assert model("fact-review-2", consistency_contract=version) is AuditedFact
        assert (
            "retained_confirmation" not in schema("fact-review-2", consistency_contract=version)["properties"]
        )
    assert (
        "retained_confirmation" in schema("fact-review-2", consistency_contract=DELTA_VERSION)["properties"]
    )
    for stage in ("fact-review", "reader-review-1"):
        assert "retained_confirmation" not in schema(stage, consistency_contract=DELTA_VERSION)["properties"]


@pytest.mark.parametrize("unknown", [False, True])
def test_native_finalization_resolves_exact_retained_evidence_and_rejects_unknown_split_ref(
    monkeypatch, unknown
):
    from podgen.runtime.evaluation.reading_audit import lifecycle
    from podgen.runtime.evaluation.reading_audit.source import AuditContext
    from tests.test_reading_audit_evidence import catalog

    materials, corpus, root, _, _, journal, index = catalog(monkeypatch)
    values = lifecycle.current_catalog(journal, root, materials, index, corpus)
    context, prior, value = setup()
    prior[0]["source_refs"] = [dict(from_review="previous", evidence_id="H1")]
    context["previous_decisions"][0]["source_refs"] = deepcopy(prior[0]["source_refs"])
    if unknown:
        value["previous_dispositions"] = [update(targets=("D1", "D2"))]
        value["audit"]["claim_checks"] = [
            deepcopy(prior[0]),
            dict(
                deepcopy(prior[0]),
                decision_id="D2",
                source_refs=[dict(from_review="previous", evidence_id="H999")],
            ),
        ]
    audit = AuditContext(context, {}, dict(H1=values["E1"]), index, DELTA_VERSION, tuple(prior))
    parsed = model("fact-review-1", consistency_contract=DELTA_VERSION).model_validate(value)
    root = dict(root, stage="fact-review-1", attempt_kind="role")
    if unknown:
        with pytest.raises(ContractError, match="audit_evidence_not_delivered"):
            lifecycle._finalize(journal, root, parsed, audit, materials, SCRIPT)
    else:
        output, proof = lifecycle._finalize(journal, root, parsed, audit, materials, SCRIPT)
        assert output["audit"]["claim_checks"][0]["source_refs"] == prior[0]["source_refs"]
        assert proof["resolved_evidence"]["previous:H1"] == values["E1"]
        assert proof["recheck_delta"]["retained_decision_ids"] == ["D1"]
        assert "retained_evidence_carried" not in proof


@pytest.mark.parametrize("verdict", ["unchecked", "unsupported", "contradicted"])
def test_global_confirmation_cannot_turn_a_prior_gap_or_defect_into_keep(verdict):
    context, prior, value = setup()
    prior[0]["verdict"] = context["previous_decisions"][0]["verdict"] = verdict
    if verdict == "unchecked":
        prior[0]["source_refs"] = context["previous_decisions"][0]["source_refs"] = []
    with pytest.raises(ContractError):
        admitted(value, context, prior)


def test_exact_delete_and_replace_windows_do_not_require_reconstructing_old_text():
    after = deepcopy(SCRIPT)
    after["utterances"] = [dict(speaker="HOST", text="A different factual account.")]
    windows, _ = comparison(SCRIPT, after)
    assert windows == [
        dict(
            kind="replace",
            before=[dict(index=i, **row) for i, row in enumerate(SCRIPT["utterances"])],
            after=[dict(index=0, **after["utterances"][0])],
        )
    ]
    windows, _ = comparison(SCRIPT, dict(utterances=[SCRIPT["utterances"][1]]))
    assert windows == [dict(kind="delete", before=[dict(index=0, **SCRIPT["utterances"][0])], after=[])]


def test_new_schema_supports_provider_nodes_and_assignment_enum():
    from podgen.writing.revision.verification import bind_assignments
    from tests.test_model_wire_schema import assert_reference_nodes

    current = bind_assignments(
        schema("fact-review-2", consistency_contract=DELTA_VERSION), dict(assignments=[dict(id="F1")])
    )
    assert_reference_nodes(current)
    assert current["$defs"]["AssignmentResult"]["properties"]["assignment_id"]["enum"] == ["F1"]


@pytest.mark.parametrize("confirmation", [False, 1, "true", None])
def test_confirmation_is_an_explicit_json_true(confirmation):
    context, prior, value = setup()
    value["retained_confirmation"]["confirmed"] = confirmation
    with pytest.raises(ValueError):
        admitted(value, context, prior)


def test_assignments_have_exact_old_quotes_and_unambiguous_current_coordinates():
    after = deepcopy(SCRIPT)
    after["utterances"].insert(0, dict(speaker="HOST", text="New opening."))
    context, _, _ = setup(after=after)
    assignment = context["assignments"][0]
    assert assignment["anchor_version"] == "previous"
    assert assignment["anchors"][0]["utterance_index"] == 0
    assert assignment["current_utterance_indices"] == [1]
    after["utterances"][1]["text"] = "A changed account."
    context, _, _ = setup(after=after)
    assignment = context["assignments"][0]
    assert assignment["change_indices"] == [0]
    assert "current_utterance_indices" not in assignment


def test_partly_changed_decision_shows_known_current_positions_without_duplicate_quotes():
    context = recheck_context(fact=True)
    context["previous_decisions"][0]["anchors"].append(
        dict(utterance_index=1, quote=SCRIPT["utterances"][1]["text"])
    )
    after = dict(
        utterances=[
            dict(speaker="HOST", text="A changed opening."),
            dict(speaker="HOST", text="An inserted link."),
            SCRIPT["utterances"][1],
        ]
    )
    attach(context, SCRIPT, after)
    row = context["previous_decisions"][0]
    assert row["requires_update"] and row["anchor_version"] == "previous"
    assert row["current_utterance_indices"] == [None, 2]
    assert "current_anchors" not in row
