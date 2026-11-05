"""artifact-jcs-v1. No Unicode normalization; unordered envelope inputs only are sorted."""

import hashlib

import rfc8785

from .contracts.common import ArtifactRef, validate_json


def canonical_bytes(value) -> bytes:
    validate_json(value)
    return rfc8785.dumps(value)


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sorted_refs(refs: list[ArtifactRef]) -> list[ArtifactRef]:
    unique = {(r.kind, str(r.artifact_id), str(r.revision_id)): r for r in refs}
    return [unique[key] for key in sorted(unique)]


def content_hash(
    kind: str, schema_version: str, payload: dict, input_refs: list[ArtifactRef], blob_hashes: list[dict]
) -> str:
    document = dict(
        kind=kind,
        schema_version=schema_version,
        payload=payload,
        input_refs=[r.model_dump(mode="json") for r in sorted_refs(input_refs)],
        blob_hashes=sorted(
            blob_hashes, key=lambda b: (b["purpose"].encode("utf-16-be"), b["hash"].encode("utf-16-be"))
        ),
    )
    return "sha256:artifact-jcs-v1:" + digest(document)
