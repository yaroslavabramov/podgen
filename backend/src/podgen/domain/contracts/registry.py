"""Single owner registry; API and model-facing schemas derive from these types."""

from dataclasses import dataclass
from typing import get_args

from . import audio, configuration, episodes, evaluation, evidence, research, verification, writing
from .common import ArtifactKind, Contract, validate_json


@dataclass(frozen=True)
class SchemaOwner:
    owner: str
    model: type[Contract]
    schema_version: str = "1.0"


REGISTRY: dict[str, SchemaOwner] = {}
for module, owner in [
    (episodes, "00"),
    (configuration, "00/06"),
    (evidence, "01"),
    (research, "02"),
    (writing, "03"),
    (verification, "04"),
    (evaluation, "04"),
    (audio, "05"),
]:
    for kind in get_args(ArtifactKind):
        model = getattr(module, kind, None)
        if model is not None:
            if kind in REGISTRY:
                raise RuntimeError(f"duplicate_schema_owner:{kind}")
            REGISTRY[kind] = SchemaOwner(owner, model)
if set(REGISTRY) != set(get_args(ArtifactKind)):
    raise RuntimeError(f"missing_schema_owners:{set(get_args(ArtifactKind)) - set(REGISTRY)}")


def validate_payload(kind: str, payload: dict, schema_version: str = "1.0") -> Contract:
    if kind not in REGISTRY or schema_version != REGISTRY[kind].schema_version:
        raise ValueError("unsupported_kind_or_schema_version")
    validate_json(payload)
    import json

    return REGISTRY[kind].model.model_validate_json(
        json.dumps(payload, ensure_ascii=False, allow_nan=False), strict=True
    )
