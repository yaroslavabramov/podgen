"""The claim-first task is fact-only and retains the canonical feedback model."""

from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.fact_context import SCHEMA_VERSION
from podgen.writing.fact_review import feedback_model
from podgen.writing.revision.contracts import FACT_VERSION

from tests.schema_cases import without_descriptions


def shape(value):
    value = without_descriptions(value)

    def walk(item):
        if isinstance(item, dict):
            return {key: sorted(child) if key == "required" else walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    return walk(value)


def test_contextual_fact_order_changes_only_tasks_and_order_not_fields_or_validators():
    assert feedback_model(SCHEMA_VERSION) is feedback_model(FACT_VERSION)
    for stage in ("fact-review", "fact-review-1", "fact-review-2"):
        old = response_schema(stage, "editorial-role-packets/1", fact_schema_version=FACT_VERSION)
        new = response_schema(stage, "editorial-role-packets/1", fact_schema_version=SCHEMA_VERSION)
        assert shape(new) == shape(old)
        assert list(new["properties"]).index("findings") < list(new["properties"]).index("important_limits")
        fields = list(new["$defs"]["FactFinding"]["properties"])
        assert fields.index("observation") < fields.index("support_labels") < fields.index("priority")
        assert (
            "proper name or number" in new["$defs"]["FactFinding"]["properties"]["observation"]["description"]
        )
    for stage in ("plan", "plan-review", "draft", "reader-review", "revise-1"):
        assert response_schema(
            stage, "editorial-role-packets/1", fact_schema_version=SCHEMA_VERSION
        ) == response_schema(stage, "editorial-role-packets/1", fact_schema_version=FACT_VERSION)
