# Canonical contracts

The [Python registry](../backend/src/podgen/domain/contracts/registry.py) defines Podgen's artifact kinds and schema versions. The [generated registry](generated/registry.json) lists them and points to the published JSON Schemas. Make schema changes in the registry and regenerate the files from the repository root.

```sh
uv run --directory backend python -m podgen.schema_export --out ../contracts/generated
node --experimental-strip-types contracts/check-golden.ts
uv run --directory backend python -m podgen.package_cli validate ../contracts/examples/research-note-package.json
```

## Canonical bytes

The Python implementation and [TypeScript implementation](canonical.ts) share [golden vectors](golden.json) for canonical JSON, hashing and Unicode ranges. Object keys use UTF-16 sort order. The envelope treats `input_refs` as a set; payload arrays retain order. Decimal values and integers outside the safe-integer range use strings. Null is distinct from an absent field. Invalid Unicode, NaN and infinity fail validation.

## Fixture packages

The [example package](examples/research-note-package.json) illustrates `podgen-fixture-v1`. Its top-level collections describe the concrete inputs and their references.

| Collection | Contents |
| --- | --- |
| `artifacts` | A package-local `key`, `kind`, complete `payload`, `input_keys`, `audience` and `identity_of`. The first version uses a null identity link. |
| `runtime_records` | A `key`, `record_kind`, complete `payload` and `audience`. |
| `blobs` | A `key` and relative `path`, with `sha256`, `byte_length` and `media_type`. |
| `external_refs`, optional `external_runtime_refs` | Exact references to existing authorized records. |

Payload entries can bind to another artifact (`{"$artifact":"key"}`), runtime record (`{"$runtime":"key"}`), blob (`{"$blob":"key"}`) or UTF-8 file (`{"$text":"relative-file.txt"}`). Files must stay inside the package root.

Offline validation checks the schemas, graph, bindings and digests. A transactional import also checks permissions, the existence of external references, selectors and relationships between dossier, plan and script. Editorial acceptance is recorded separately. For raw Markdown and text cases, see the [editorial staging guide](../backend/README.md#try-the-storage-and-editorial-contracts).
