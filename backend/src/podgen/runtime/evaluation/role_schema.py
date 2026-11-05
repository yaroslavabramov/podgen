"""Versioned model-facing descriptions, independent of the persisted output contract."""

from .workflow import output_model


def response_schema(stage, input_contract):
    schema = output_model(stage).model_json_schema()
    if input_contract == "editorial-role-packets/1":
        definitions = schema.get("$defs", {})
        if "PlanningAct" in definitions:
            definitions["PlanningAct"]["properties"]["act_id"]["description"] = "Short local act label."
        if "PlanningHostFunction" in definitions:
            definitions["PlanningHostFunction"]["properties"]["speaker_id"]["description"] = (
                "Exact speaker label from the assignment."
            )
    return schema
