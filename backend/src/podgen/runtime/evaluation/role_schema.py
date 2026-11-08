"""Versioned model-facing semantics; persisted fields and historical schemas retain their identities."""

from podgen.writing.fact_review import describe_schema as describe_fact_schema
from podgen.writing.plan_review_descriptions import describe_plan_review
from podgen.writing.planning_descriptions import describe_selected_story
from podgen.writing.planning_schema import planning_schema
from podgen.writing.projection_versions import CURRENT, V1, V2, V6, V7, V8, V9, historical, validate_version
from podgen.writing.reader_schema import describe_reader

from .reading_workflow import base_stage
from .workflow import output_model

DESCRIPTIONS = {
    "PlanProposal": {"listener_promise": "Understanding promised to this audience, with its scope."},
    "PlanningOpening": {
        "approach": "The opening's explanatory purpose and entry point; the writer chooses its spoken wording.",
        "question_to_open": "The unresolved listener question, independent of which speaker voices it.",
    },
    "PlanningTransitions": {
        "from_previous": "Inference connecting this part to what precedes it.",
        "toward_next": "Inference or unresolved need motivating the next part.",
    },
    "PlanningEnding": {
        "answer_to_deliver": "The conclusion the listener should understand, with its essential scope: planning meaning, not a ready closing speech."
    },
    "PlanningHostFunction": {
        "contribution": "Reasoning and explanatory work owned by this speaker.",
        "interaction_guidance": "How this contribution develops others' reasoning, or advances a solo explanation. Describe its purpose, not prescribed lines or a recurring reaction.",
    },
}


def response_schema(
    stage, input_contract, *, projection_version=CURRENT, reader_schema_version=None, fact_schema_version=None
):
    schema = output_model(
        stage,
        reader_schema_version=reader_schema_version,
        fact_schema_version=fact_schema_version,
        projection_version=projection_version if input_contract == "editorial-role-packets/1" else V1,
    ).model_json_schema()
    if base_stage(stage) == "reader-review":
        describe_reader(schema, reader_schema_version)
    schema = planning_schema(
        schema, legacy=input_contract != "editorial-role-packets/1" or historical(projection_version)
    )
    if input_contract != "editorial-role-packets/1":
        return describe_fact_schema(schema, base_stage(stage), fact_schema_version)
    validate_version(projection_version)
    definitions = schema.get("$defs", {})
    if "PlanningAct" in definitions:
        definitions["PlanningAct"]["properties"]["act_id"]["description"] = "Short local act label."
    if "PlanningHostFunction" in definitions:
        definitions["PlanningHostFunction"]["properties"]["speaker_id"]["description"] = (
            "Exact speaker label from the assignment."
        )
    if projection_version == V1:
        return describe_fact_schema(schema, base_stage(stage), fact_schema_version)
    if projection_version not in (V1, V2) and "PlanProposal" in definitions:
        definitions["PlanProposal"]["properties"]["main_question"]["description"] = (
            "Central question of the episode for this audience; the opening may raise the same question or a local one."
        )
    for name, fields in DESCRIPTIONS.items():
        if name in definitions:
            for field, description in fields.items():
                definitions[name]["properties"][field]["description"] = description
    if stage == "review":
        schema["properties"]["listener_answer"]["description"] = (
            "Listener understanding reconstructed from the actual written speech, including its uncertainty."
        )
    elif stage.startswith("plan-review"):
        schema["properties"]["listener_answer"]["description"] = (
            "Listener understanding this proposed reasoning would enable."
        )
    if projection_version in (V6, V7, V8) and stage in ("plan", "plan-revise"):
        describe_selected_story(schema)
    if projection_version == V9 and stage in ("plan", "plan-revise"):
        from podgen.writing.planning_v9 import describe_plan

        describe_plan(schema)
    if projection_version in (V8, V9) and stage.startswith("plan-review"):
        describe_plan_review(schema)
        if projection_version == V9:
            from podgen.writing.planning_v9 import describe_review

            describe_review(schema)
    return describe_fact_schema(schema, base_stage(stage), fact_schema_version)
