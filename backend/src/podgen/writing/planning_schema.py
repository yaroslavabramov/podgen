"""One compatibility model, with historical strict wires and a current checklist-free wire."""

LEGACY_FIELDS = {
    "PlanningAct": "limitations_to_voice",
    "PlanningEnding": "qualifications",
    "PlanningOutput": "limitations",
    "PlanReviewOutput": "important_limits",
}


def planning_schema(schema, *, legacy):
    for name, field in LEGACY_FIELDS.items():
        definition = schema if schema.get("title") == name else schema.get("$defs", {}).get(name)
        if definition is None:
            continue
        properties = definition["properties"]
        required = set(definition.get("required", ()))
        if legacy:
            required.add(field)
        else:
            properties.pop(field)
            required.discard(field)
        definition["required"] = [key for key in properties if key in required]
    return schema
