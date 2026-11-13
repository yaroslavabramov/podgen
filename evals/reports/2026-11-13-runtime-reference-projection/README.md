# Runtime reference projection and evaluator publication

Migration `0025_runtime_references` replaces per-event project payload scans with
an immutable typed reference projection. `protected_hashes` computes the same
forward/byte-alias fixed point using indexed frontier queries. Classification and
privacy withdrawal insert bounded batches. No authorization decision is cached.

## Publication and compatibility

- `runtime_artifact_refs` stores owner project/record and the full
  `artifact_id, revision_id, kind` tuple. The target has no local-project FK:
  published catalog references may belong to another project.
- `runtime_runtime_refs` stores owner project/record and
  `target_record_id, record_kind`.
- `runtime_blob_refs` stores owner project/record and all four BlobRef fields.
- Owner-prefixed primary keys serve read closure; reverse target/SHA indexes serve
  forward privacy closure. Existing artifact, original-input, revalidation and
  published-catalog projections remain canonical; they are not duplicated.
- An AFTER INSERT trigger publishes the runtime and projection in one transaction.
  Existing application writers, package import and raw INSERT routes use it.
  Migration locks runtime writes while installing the trigger and backfilling
  existing rows. A failed transaction leaves neither record nor projection.
- Backfill runs once on the server. Its materialized intermediate can spill under
  PostgreSQL work_mem; the application does not download historical payloads.
  Migration therefore needs a write-lock window proportional to existing data.

The SQL traversal recognizes the same exact key sets as `walk_values` and stops
at a recognized reference. Diagnostic aliases, arbitrary UUIDs and serialized
JSON strings are not references. Full ref tuples remain available to read-side
identity validation. The projection is structural, not a new permission model.

### Raw JSON and U+0000

PostgreSQL `json_each` and `json_object_keys` decode scalar escapes even when the
scalar is unrelated text. Keeping the column as `json` alone does not prevent
failure on legitimate escaped U+0000 in original source text.

`podgen_runtime_reference_document` creates a temporary traversal view. Two
regular expressions recognize complete JSON string tokens with an active NUL
escape: values become null; keys remain unknown non-reference keys. Escaped
backslash pairs are consumed together, so literal `\\u0000` is untouched. This
view is never persisted. Canonical payload bytes remain exactly as inserted.

No valid SQL-backed reference value can contain NUL: UUIDs, SHA and lengths have
restricted types; the remaining identity fields must match PostgreSQL text
metadata, which cannot hold NUL. A malformed raw INSERT with NUL in an exact ref
fails the projection's NOT NULL constraint instead of silently changing the ref.
Tests cover multiple backslash counts, escaped quotes beside literal escapes,
NUL in neighboring text/keys, exact raw UTF-8 bytes, and rollback of malformed refs.

## Closure and membership invariants

Traversal follows consumers, owned blobs, byte-identical aliases and their
consumers. It never moves from an evaluator consumer backward into its primary
inputs. Both original and revalidated provenance remain effective. Catalog asset
SHA aliases participate in both directions exactly as before.

Each reached identity is expanded once. Frontier parameters and server fetch
buffers contain at most 512 identities/rows; retained memory contains reached
IDs/hashes, not the entire project or payloads. A shared count-response blob can
legitimately connect a large part of history. That reachable component is still
traversed completely, including already classified hashes and late aliases.

`evaluator_protected_hashes(project_id, sha256)` is an immutable publication
membership index, not an authorization cache. Historical witness rows remain.
Authorization still reads `evaluator_blob_classifications`.

A BEFORE INSERT trigger atomically reserves membership and inserts its witness.
An existing reservation suppresses a duplicate witness, including from old
writers. If witness insertion fails, the reservation rolls back in the same
statement. Concurrent old/new writers publish one witness. New batch reservations
sort SHA globally and inside SQL to avoid opposite acquisition orders. No
traversal early-stop relies on this index.

## Whole append-boundary measurements

`measure_publication.py` invokes the real `runtime_records.append_runtime`, with
ACL, reference identity checks, publication, classification and SQL triggers.
The event has either one BlobRef or no refs. This is **not** the complex runtime
input graph of a whole episode; the separate read-authorization work is necessary
for that case. The application transaction wrapper's begin/rollback is outside
the measured interval. No network/model/object-store calls occur.

Baseline classifier/closure are loaded directly from `a5e8d314`. In its isolated,
rolled-back transaction only the new projection and membership triggers are
disabled. All graph seeding is outside the measured interval. New trials use the
actual new triggers. Unrelated records contain 4 KiB text plus a typed BlobRef in
a disjoint alias component, so both projection and payload history grow.

First publication:

| Event refs | Unrelated records | Reachable pre-existing records | Client SQL before → after | Selected JSON value bytes before → after | Time ms before → after |
|---|---:|---:|---:|---:|---:|
| One BlobRef | 64 | 32 | 46 → 12 | 306,558 → 8,710 | 31.32 → 14.60 |
| One BlobRef | 1,024 | 32 | 46 → 12 | 4,561,278 → 8,710 | 127.54 → 13.53 |
| One BlobRef | 1,024 | 320 | 334 → 12 | 4,761,438 → 81,574 | 270.06 → 42.12 |
| Empty | 1,024 | 0 | 10 → 5 | 4,560,737 → 34 | 115.48 → 3.12 |

The empty-event project contains the same 32-record component, but the event
cannot reach it. New traversal reads zero full runtime payloads in every case.
The second identical-byte event creates **zero** new classification witnesses;
baseline creates another 33 or 321 respectively. The initial new publication
creates the same 33/321 witnesses as baseline, plus one membership row per SHA.

Counts are client SQL statements/round trips, including the entire append
boundary; trigger-internal statements execute on the server. Selected-value
bytes are the JSON encoding size of consumed row values, **not TCP wire bytes**.
Times are one local PostgreSQL sample with tracemalloc and row instrumentation;
no claim of production latency percentiles. Memory figures in measurement.json
are Python allocations, not server memory or libpq buffers.

## Validation

`validation.txt`: **23 passed**. Coverage includes 100 generated graph parity
cases, full SQL forward/alias/revalidation/catalog semantics, unchanged primary
input access, late aliases, historical backfill, old INSERT publication,
projection rollback, witness failure rollback, mixed old/new concurrency,
repeat-row growth, and existing evaluator/privacy/catalog regressions.

Reproduce on an isolated migrated database (no API credentials required):

```sh
PYTHONPATH=backend/src:backend backend/.venv/bin/python \
  evals/reports/2026-11-13-runtime-reference-projection/measure_publication.py \
  --state /path/to/private-db-state.json --output /path/to/measurement.json
```

The state file supplies `database_url`; do not commit it. Run the listed pytest
files with `PODGEN_TEST_DATABASE_URL` pointing to the same isolated database.
