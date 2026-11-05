"""Authoring packages are import templates, never an alternate persistence wire format."""

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, JsonValue

from .canonical import canonical_bytes
from .contracts.common import ArtifactKind, ArtifactRef, BlobRef, Contract, RuntimeRef, Text
from .contracts.registry import validate_payload
from .errors import ContractError
from .references import check_subtype, walk_values


class PackageArtifact(Contract):
    key: Text
    kind: ArtifactKind
    payload: dict[str, JsonValue]
    input_keys: list[str]
    audience: Literal["project", "evaluator"]
    identity_of: str | None


class PackageBlob(Contract):
    key: Text
    path: Text
    media_type: Text
    sha256: Text
    byte_length: int


class PackageRuntime(Contract):
    key: Text
    record_kind: Literal[
        "acquisition_record",
        "search_record",
        "tool_execution",
        "context_assembly",
        "user_input",
        "evaluation_reading",
    ]
    payload: dict[str, JsonValue]
    audience: Literal["project", "evaluator"]


class ArtifactPackage(Contract):
    format: Literal["podgen-fixture-v1"]
    package_id: UUID
    test_data: bool
    description: Text
    artifacts: list[PackageArtifact]
    runtime_records: list[PackageRuntime]
    blobs: list[PackageBlob]
    external_refs: dict[str, ArtifactRef]
    external_runtime_refs: dict[str, RuntimeRef] = Field(default_factory=dict)


def relative_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if Path(value).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ContractError("package_path_outside_root")
    return path


def load_package(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("duplicate_json_key")
            result[key] = value
        return result

    return ArtifactPackage.model_validate(json.loads(path.read_text(), object_pairs_hook=unique))


def substitute(value, artifacts, runtimes, blobs, root):
    if isinstance(value, dict):
        for marker, lookup in [("$artifact", artifacts), ("$runtime", runtimes), ("$blob", blobs)]:
            if marker in value:
                if set(value) != {marker} or value[marker] not in lookup:
                    raise ContractError("unknown_or_malformed_package_binding", str(value))
                return lookup[value[marker]].model_dump(mode="json")
        if "$text" in value:
            if set(value) != {"$text"}:
                raise ContractError("malformed_text_binding")
            return relative_path(root, value["$text"]).read_bytes().decode("utf-8")
        return {k: substitute(v, artifacts, runtimes, blobs, root) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, artifacts, runtimes, blobs, root) for v in value]
    return value


def package_nodes(package):
    """One graph for artifact AND runtime nodes; mixed cycles cannot bypass ordering."""
    artifacts = {a.key: a for a in package.artifacts}
    runtimes = {r.key: r for r in package.runtime_records}
    items = {**artifacts, **runtimes}
    keys = (
        [a.key for a in package.artifacts]
        + [r.key for r in package.runtime_records]
        + [b.key for b in package.blobs]
        + list(package.external_refs)
        + list(package.external_runtime_refs)
    )
    if len(keys) != len(set(keys)):
        raise ContractError("duplicate_package_key")

    def bindings(value):
        if isinstance(value, dict):
            for marker, allowed in [
                ("$artifact", set(artifacts) | set(package.external_refs)),
                ("$runtime", set(runtimes) | set(package.external_runtime_refs)),
            ]:
                if marker in value:
                    if set(value) != {marker} or value[marker] not in allowed:
                        raise ContractError(
                            "unknown_artifact_key" if marker == "$artifact" else "unknown_runtime_key"
                        )
                    yield value[marker]
                    return
            for child in value.values():
                yield from bindings(child)
        elif isinstance(value, list):
            for child in value:
                yield from bindings(child)

    dependencies = {}
    for key, item in items.items():
        deps = set(bindings(item.payload))
        if isinstance(item, PackageArtifact):
            if set(item.input_keys) - set(artifacts) - set(package.external_refs):
                raise ContractError("unknown_artifact_key", key)
            if item.identity_of and item.identity_of not in set(artifacts) | set(package.external_refs):
                raise ContractError("unknown_artifact_key", key)
            deps |= set(item.input_keys) | ({item.identity_of} if item.identity_of else set())
        dependencies[key] = deps
    ordered, done = [], set(package.external_refs) | set(package.external_runtime_refs)
    while len(ordered) < len(items):
        available = [k for k in items if k not in done and dependencies[k] <= done]
        if not available:
            raise ContractError("cyclic_package_references")
        for key in available:
            ordered.append(items[key])
            done.add(key)
    return ordered


def package_order(package):
    return [node for node in package_nodes(package) if isinstance(node, PackageArtifact)]


def validate_package(package, root: Path):
    order = package_order(package)
    refs = dict(package.external_refs)
    for item in order:
        identity = (
            refs[item.identity_of].artifact_id
            if item.identity_of
            else uuid5(package.package_id, "artifact:" + item.key)
        )
        refs[item.key] = ArtifactRef(
            artifact_id=identity,
            revision_id=uuid5(package.package_id, "revision:" + item.key),
            kind=item.kind,
        )
    runtime_refs = dict(package.external_runtime_refs) | {
        r.key: RuntimeRef(record_kind=r.record_kind, record_id=uuid5(package.package_id, "runtime:" + r.key))
        for r in package.runtime_records
    }
    blob_refs = {}
    for blob in package.blobs:
        data = relative_path(root, blob.path).read_bytes()
        if len(data) != blob.byte_length or hashlib.sha256(data).hexdigest() != blob.sha256:
            raise ContractError("package_blob_integrity_error", blob.key)
        blob_refs[blob.key] = BlobRef(
            storage_key="validation-only/" + blob.key,
            sha256=blob.sha256,
            byte_length=blob.byte_length,
            media_type=blob.media_type,
        )
    by_id = {r.revision_id: key for key, r in refs.items()}
    bodies = {item.key: substitute(item.payload, refs, runtime_refs, blob_refs, root) for item in order}
    for item in order:
        body = bodies[item.key]
        validate_payload(item.kind, body)
        for field, _, ref in walk_values(body):
            if isinstance(ref, ArtifactRef):
                if ref.revision_id not in by_id or refs[by_id[ref.revision_id]] != ref:
                    raise ContractError("undeclared_package_reference", item.key)
                target_key = by_id[ref.revision_id]
                if target_key in bodies:
                    check_subtype(field, bodies[target_key])
        if item.identity_of and refs[item.identity_of].kind != item.kind:
            raise ContractError("identity_kind_mismatch")
    for record in package.runtime_records:
        canonical_bytes(substitute(record.payload, refs, runtime_refs, blob_refs, root))
    return dict(
        format=package.format,
        test_data=package.test_data,
        artifacts=len(order),
        blobs=len(blob_refs),
        checks=[
            "canonical payload schemas",
            "nominal reference kinds",
            "configuration subtypes",
            "declared reference closure",
            "acyclic order",
            "full text reads",
            "blob sizes and SHA-256",
        ],
        pending=[
            "repository import: ACL, scoped FK, exact selectors, dossier and script bindings",
            "owner semantic review and expert acceptance",
        ],
    )
