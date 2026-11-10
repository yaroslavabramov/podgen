"""Only the contextual ending descriptions change; the published /3 wire remains intact."""

from copy import deepcopy

from podgen.writing import ending_guidance
from podgen.writing.ending_context_descriptions import REQUESTED_CHANGE, REVIEW_FIELDS, WHY

from tests.test_neutral_planner_ending import schema


def test_exact_contextual_delta_keeps_all_other_schema_content_and_roles():
    for stage in ("plan", "plan-revise", "plan-review", "plan-review-revised"):
        old = schema(stage, ending_guidance.NEUTRAL_VERSION)
        expected = deepcopy(old)
        if stage in ("plan", "plan-revise"):
            expected["$defs"]["PlanningFinalMoment"]["properties"]["why_end_here"]["description"] = WHY
        else:
            for field, description in REVIEW_FIELDS.items():
                expected["properties"][field]["description"] = description
            expected["$defs"]["PlanReviewFinding"]["properties"]["requested_change"]["description"] = (
                REQUESTED_CHANGE
            )
        assert schema(stage, ending_guidance.CONTEXT_VERSION) == expected
    for stage in ("draft", "revise", "fact-review", "reader-review"):
        assert schema(stage, ending_guidance.CONTEXT_VERSION) == schema(stage, ending_guidance.NEUTRAL_VERSION)
