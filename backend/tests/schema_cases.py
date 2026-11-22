"""Compare field tasks independently of the published shape and property order."""


import hashlib
import json


def wire_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def without_descriptions(value):
    if isinstance(value, dict):
        return {key: without_descriptions(child) for key, child in value.items() if key != "description"}
    if isinstance(value, list):
        return [without_descriptions(child) for child in value]
    return value
