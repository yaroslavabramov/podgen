"""Reproducible generated schemas and cross-language hash fixtures."""

import argparse
import json
from pathlib import Path

from podgen.domain import editorial_staging, packages
from podgen.domain.canonical import canonical_bytes, content_hash
from podgen.domain.contracts import REGISTRY, common, runtime


def export(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    manifest = {}
    schemas = {}
    for kind, owner in sorted(REGISTRY.items()):
        schema = owner.model.model_json_schema(by_alias=True)
        schema["$id"] = f"urn:podgen:{kind}:1.0"
        schemas[kind] = schema
        manifest[kind] = dict(
            owner=owner.owner, schema_version=owner.schema_version, file=f"{kind}.schema.json"
        )
    for module in (common, runtime, packages, editorial_staging):
        for name, model in vars(module).items():
            if (
                isinstance(model, type)
                and issubclass(model, common.Contract)
                and model.__module__ == module.__name__
            ):
                schemas.setdefault(name, model.model_json_schema(by_alias=True))
    for name, schema in schemas.items():
        (root / f"{name}.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    (root / "registry.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # API and model tool views reference the SAME canonical definitions.
    (root / "api-and-tools.json").write_text(
        json.dumps(
            {
                name: {
                    "api_payload_schema": f"{name}.schema.json",
                    "model_payload_schema": f"{name}.schema.json",
                }
                for name in manifest
            },
            indent=2,
        )
        + "\n"
    )
    return len(schemas)


def golden(path: Path):
    refs = [
        dict(
            kind="ResearchNote",
            artifact_id="00000000-0000-4000-8000-000000000001",
            revision_id="00000000-0000-4000-8000-000000000002",
        )
    ]
    payloads = [
        {"answer": "Русский 🛰️ е\u0301 𐀀", "decimal": "9007199254740993.00000000001", "value": None},
        {"numbers": [0.0, -0.0, 0.000001, 1e-7, 4.5, 333333333.3333333], "absent_is_not_null": {}},
        {"\ue000": "BMP", "😀": "astral", "a": '\b\t\n\f\r"\\', "ordered": ["second", "first"]},
        {"value": None},
        {},
    ]
    cases = []
    from podgen.domain.contracts import ArtifactRef

    for payload in payloads:
        document = dict(
            kind="ResearchNote",
            schema_version="1.0",
            payload=payload,
            input_refs=refs,
            blob_hashes=[dict(purpose="/body", hash="a" * 64)],
        )
        checksum = content_hash(
            "ResearchNote",
            "1.0",
            payload,
            [ArtifactRef.model_validate(r) for r in refs],
            document["blob_hashes"],
        )
        cases.append(
            dict(document=document, canonical=canonical_bytes(document).decode(), content_hash=checksum)
        )
    path.write_text(
        json.dumps(
            dict(
                profile="artifact-jcs-v1",
                cases=cases,
                selectors=[dict(text="A🛰️е\u0301Z", start=1, end=5, exact="🛰️е\u0301")],
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("../contracts/generated"))
    args = parser.parse_args()
    print(f"Exported {export(args.out)} schemas")
    golden(args.out.parent / "golden.json")
