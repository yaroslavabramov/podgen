"""Schemas are opaque archived documents, not graphs of runtime references."""

from podgen.domain.canonical import canonical_bytes


def schema_blob(journal, model):
    return journal.blob(canonical_bytes(model.model_json_schema()), "application/schema+json")
