"""Compare field tasks independently of the published shape and property order."""


def without_descriptions(value):
    if isinstance(value, dict):
        return {key: without_descriptions(child) for key, child in value.items() if key != "description"}
    if isinstance(value, list):
        return [without_descriptions(child) for child in value]
    return value
