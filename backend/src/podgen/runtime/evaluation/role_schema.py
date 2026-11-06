"""Versioned model-facing semantics; persisted fields and historical schemas retain their identities."""

from podgen.writing.projection_versions import CURRENT, V1, V2, validate_version

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


def response_schema(stage, input_contract, *, projection_version=CURRENT):
    schema = output_model(stage).model_json_schema()
    if input_contract != "editorial-role-packets/1":
        return schema
    validate_version(projection_version)
    definitions = schema.get("$defs", {})
    if "PlanningAct" in definitions:
        definitions["PlanningAct"]["properties"]["act_id"]["description"] = "Short local act label."
    if "PlanningHostFunction" in definitions:
        definitions["PlanningHostFunction"]["properties"]["speaker_id"]["description"] = (
            "Exact speaker label from the assignment."
        )
    if projection_version == V1:
        return schema
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
    return schema
