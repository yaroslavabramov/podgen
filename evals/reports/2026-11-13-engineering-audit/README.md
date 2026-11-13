# Engineering audit fixes — 2026-11-13

Scope: the six paths identified in the supplied audit. This is not a claim that
every subsystem in the repository has been audited. No paid model calls or
episode generation were performed. Heavy database and inference checks ran
serially.

## Status

All six changes are integrated and verified. The final selected regression set
contains **201 distinct tests**, with no unresolved failures. The first
candidate's 155,923 SQL statements per operation was rejected as insufficient;
the accepted result is 1,345.

## Changes and evidence

| Audit finding | Change | Evidence |
|---|---|---|
| Repeated material/dependency walks | Batched reachable graph facts, bounded authorization before publication, frozen-input reauthorization around external work | Same 51-source cycle: 324,255→1,345 SQL statements |
| Duplicate native token counts | Reuse a persisted receipt bound to the exact native request and provider configuration | Count→mock submit→read operation uses one count instead of two; mismatched receipts are rejected |
| Per-run document reembedding | Project-local content/configuration identity; separate run membership and source labels | Actual ONNX second run embeds 0 document chunks instead of 78 |
| Recovery reads all events | Indexed turn projection, selected before full event loading; old-writer trigger and backfill | Same selected 5 events out of 26 turns; 93 SQL statements instead of 1,946,286 |
| Raw envelopes in model history | Versioned semantic history with action, arguments once, result and evidence references; unchanged raw archive | All 18 actions' arguments/results preserved in a saved 40-message history; 159,025→139,172 UTF-8 bytes |
| S3 duplicate objects/full downloads | Deterministic conditional publication, prelookup, bounded staging and server-checksum validation | 64 MiB duplicate: 0 upload/0 download instead of 64/128 MiB; one final object instead of two |

History byte counts are not token or billing measurements. Old manifests without
the new history-view pin keep their original tool definitions and replay behavior.

### Complete operation

The isolated copy contains 51 actual saved source documents. The measured cycle
includes context counting, a mock generation submit and a source read. External
providers are replaced with counting doubles; database/storage work is real and
measured. These are single samples, not latency distributions.

| Measurement | Baseline | First candidate (rejected) | Final |
|---|---:|---:|---:|
| Cycle SQL statements | 324,255 | 155,923 | 1,345 |
| Cycle seconds | 32.53 | 17.79 | 1.68 |
| Native counts / generation submissions | 2 / 1 | 1 / 1 | 1 / 1 |
| Selected-turn recovery SQL statements | 1,946,286 | 8,283 | 93 |
| Selected-turn recovery seconds | 174.04 | 0.764 | 0.098 |

The intermediate result exposed two additional causes inside the original first
finding: common dependency ancestors were authorized repeatedly, and evaluator
publication scanned all project runtime payloads and inserted repeated witnesses.
The batched candidate corrects both. Runtime authorization reads typed references
and short headers; full bodies are loaded only on explicit reads. A metadata-only
header has the same shape both inside and outside a bounded operation.

[Operation procedure and limits](operation-verification.md),
[baseline](operation-baseline.json), and [final measurement](operation-final.json)
are retained alongside a portable runner. Corpus initialization, search/indexing
and provider latency are outside this operation measurement.

Process peak RSS increased in this sample (155.3→191.2 MB); this is not a memory
improvement claim. The final sample includes phase-level SQL instrumentation.
Operation-only allocation and database-process memory were not measured here.

### Integration and subsystem checks

The first combined integration run passed 130 tests at `34679459`, before the
final graph changes. Final integration applied every migration to a fresh
PostgreSQL database and selected 201 tests. At `397203aa`, 174 passed and 27 failed
because the storage test double did not recognize the new combined identity
query. Real PostgreSQL, graph, replay, retrieval and SDK transport checks passed.

Commit `8a13d36f` updated only that test double and its exact SQL-count assertion,
preserving the old-query branches used by baseline measurements. All 36 storage
checks then passed, including the previously failed 27. **174 + 27 = 201 distinct
passing checks**; overlapping reruns are not added to this total. Production code
was unchanged after the full integration run. Retained outputs:
[initial integration](integration-initial.txt), [final storage checks](storage-final.txt).

Coverage includes old request replay, per-turn recovery, count receipt reuse,
bounded history reads, withdrawn permissions, catalog access, cyclic privacy
closure, concurrent publication, migration/backfill, old writers, unchanged raw
JSON, accepted scalar reference formats, and cleanup after snapshot-close errors.

Additional subsystem procedures, raw data and limits:

- [Reusable search index](../2026-11-13-reusable-source-index/validation.md):
  67 distinct checks; actual installed ONNX weights and exact retained originals;
  second-run elapsed 9.606→4.667 s, with query embedding and reranking still running.
  No first-build speedup is claimed.
- [Blob publication](../2026-11-13-s3-publication/README.md): streaming size and
  concurrency checks, actual boto3 transport against local HTTP, plus root's real
  PostgreSQL integration. No real cloud endpoint was exercised here.
- [Runtime reference projection](../2026-11-13-runtime-reference-projection/README.md):
  23 checks, actual PostgreSQL, old writers and historical backfill. With 32
  reachable and 1,024 unrelated records, a complete simple append falls from
  46 to 12 client SQL statements; repeated witness rows fall from 33 to zero.
  This append has one BlobRef, not a complex episode input graph.
- [History view measurement](history-view-measurement.json): exact saved request
  identity and byte counts; full source/action semantics checked for equality.

## Compatibility and remaining operational limits

Raw semantic data and historical objects remain intact. Changed model-facing
history is pinned in new manifests. Database projections are backfilled while
historical payloads remain unchanged. Source permissions and revocation must be
checked afresh at public reads and external-action boundaries.

The audit's separate observation about quotations repeated across rewrite-input
sections was not changed in this infrastructure patch. No writer prompt, model,
or editorial-process redesign is claimed.

Deterministic S3 publication does not garbage-collect historical orphan objects.
An interrupted publication can leave one unregistered deterministic object for
retry, and process death can leave staging files or incomplete multipart uploads;
deployment lifecycle cleanup remains necessary. See
[blob publication](../../../../docs/podcast-production/blob-publication.md).

The English [working agreement](../../../../AGENTS.md) now requires whole-path
review, representative operation measurements, explicit ownership/lifetimes,
concurrency/retry checks, and coherent commits. Passing isolated tests alone is
explicitly insufficient evidence of readiness.

Changes were committed in separate reviewed units. Unrelated working-tree files
were preserved. The task-owned integration PostgreSQL and the index-validation
server were stopped; test data and reports were retained. The pre-existing source
server was left unchanged. No generation was resumed.
