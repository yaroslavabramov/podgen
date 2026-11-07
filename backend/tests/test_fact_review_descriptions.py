"""The measured fact form changes assessment semantics, not persisted review fields."""

from copy import deepcopy

from podgen.writing.fact_review import FactFeedback, feedback_model
from podgen.writing.fact_review_descriptions import describe_fact_review
from podgen.writing.revision.contracts import FACT_VERSION


def test_fact_form_matches_the_measured_descriptions_and_order_without_changing_constraints():
    original = feedback_model(FACT_VERSION).model_json_schema()
    schema = deepcopy(original)
    describe_fact_review(schema)

    expected_order = [
        "reading_mode",
        "listener_answer",
        "important_limits",
        "findings",
        "research_requests",
        "strengths",
        "duration_note",
        "revision_scope",
        "recommendation",
    ]
    assert list(schema["properties"]) == schema["required"] == expected_order
    assert set(schema["required"]) == set(original["required"])

    descriptions = {
        ("properties", "listener_answer"): (
            "State the material assertions as a listener would hear them. Keep the scope of the spoken wording. "
            "An unstated source qualification may affect your assessment."
        ),
        ("properties", "important_limits"): (
            "Private evidence comparison. For consequential numbers, causal links or attributed details in the "
            "speech, identify any difference between what was counted, observed or reported and the extent of "
            "the spoken claim. A record, sample or witness may cover less than the sentence implies. Findings "
            "are for differences that change meaning; these checks are not lines for the narrator."
        ),
        ("properties", "strengths"): "Supported wording that matters to a correction. May be empty.",
        ("$defs", "FactFinding", "properties", "observation"): (
            "Exact difference between what the spoken claim implies and what the cited evidence establishes, "
            "and why it matters here."
        ),
        ("$defs", "FactFinding", "properties", "requested_change"): (
            "What must become true in the repaired account. Leave the writer free to cut a dispensable claim. "
            "When the claim needs to stay, specify the smallest accurate meaning."
        ),
    }
    for path, expected in descriptions.items():
        actual_field, old_field = schema, original
        for key in path:
            actual_field, old_field = actual_field[key], old_field[key]
        assert actual_field["description"] == expected
        if "description" in old_field:
            actual_field["description"] = old_field["description"]
        else:
            del actual_field["description"]
    schema["required"] = original["required"]
    assert schema == original


def test_describing_one_schema_is_idempotent_and_does_not_change_models_or_other_snapshots():
    historical = FactFeedback.model_json_schema()
    historical_snapshot = deepcopy(historical)
    model = feedback_model(FACT_VERSION)
    original = model.model_json_schema()
    current = deepcopy(original)
    describe_fact_review(current)
    described = deepcopy(current)
    describe_fact_review(current)
    assert current == described
    assert model.model_json_schema() == original
    assert historical == historical_snapshot == FactFeedback.model_json_schema()
