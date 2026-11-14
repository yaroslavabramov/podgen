"""Physical state has its own contract, independent of semantic attempt fields."""

FIELDS = (
    "project_id",
    "root_invocation_id",
    "turn_index",
    "invocation_id",
    "status",
    "request_record_id",
    "provider_response_id",
    "input_tokens",
    "output_tokens",
)


def descriptor(row):
    return {key: row[key] for key in FIELDS}


def invocation(root, row):
    result = dict(root)
    result.update(row)
    result.update(stage=root["stage"], attempt_index=0, output_record_id=None)
    return result
