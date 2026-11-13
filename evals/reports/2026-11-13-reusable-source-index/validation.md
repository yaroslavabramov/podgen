# Reusable document index validation — 2026-11-13

Implementation: `3779d469` on `codex/reusable-document-index-20261113`, based on
`b3d49764`. Test/measurement additions accompany this report. Migration 0024
follows the independently owned `0023_turn_event_selection`; that file was copied
only for local migration and is not part of this change.

## Scope and result

Document chunks/vectors and durable build state are now project-local, keyed by
exact text/configuration identity. Run scopes retain independent S labels,
authorization dependencies and immutable search result pages. No prompts,
provider calls, model-facing IDs or pinned requests changed.

Both counted inference and installed real ONNX weights demonstrate **zero repeated
document embeddings in a second writing run**. Tokenization is also skipped.
The first query still requires a query embedding, exhaustive authorized search
and reranking; these computations are not claimed to disappear.

## Addressed checks

* 40 cases passed in 5.82 s: reusable document tests, existing streaming SQL tests
  (including the >64 docs / >2M characters / >12K chunks case), legacy v1 SQL/ACL
  tests, and historical retrieval profile replay.
* 25 cases passed in 0.91 s: source search release/compiler and research navigation
  request replay tests.
* After adding two focused withdrawal cases, the current reusable document file
  passed all 18 cases in 1.56 s. Sixteen of these overlap the first run; there are
  **67 distinct cases**, not 83 independent tests.
* Ruff, syntax and `git diff --check` passed. No tests were run concurrently with
  another heavy suite; the slot has been returned.

The tests verify reused vectors across runs and local aliases, different text at
identical added S IDs, immutable same-run bindings, invalidation by chunking /
encoder hash / tokenizer hash / prefix / pooling / text changes, cross-project
isolation, interrupted shared builds resuming without recomputing committed
batches, incomplete documents hidden from all SQL branches, competing builders,
old complete/partial v2 projections imported with their cursor, stable persisted
pages through corpus expansion, and actual PostgreSQL withdrawal behavior.

Withdrawal tests cover document hash **and a distinct scope dependency**, access
to a previously pinned result version after revocation, membership removal,
stale append/rebuild, and revocation committing while inference is paused in
another thread. Shared chunks are removed and the builder cannot republish them.

## Representative counted-double operation

62 distinct documents, **881,277 Unicode characters**. Exact preloaded synthetic
texts. Actual PostgreSQL adapters and SQL ranking; explicit deterministic
inference double. Each measured operation includes index construction and its
first search, including creation of the immutable run scope. The historical
implementation is loaded verbatim from baseline Git sources, not recreated.

| Operation | SQL statements | Document chunks embedded | Embedding batches | Stored document-vector rows | Elapsed s |
|---|---:|---:|---:|---:|---:|
| Baseline first run | 4,434 | 620 | 62 | 620 | 1.959 |
| Baseline second run | 4,434 | 620 | 62 | 1,240 | 1.843 |
| Reusable first run | 2,543 | 620 | 62 | 620 | 1.678 |
| Reusable second run | 1,055 | 0 | 0 | 620 | 0.387 |

Vector payload storage after two runs is 954,800 bytes for the reusable projection
versus 1,909,600 bytes for baseline per-run rows (`sum(pg_column_size(embedding))`).
The comparison keeps baseline legacy rows alongside the new projection to measure
both implementations; their presence is not a new per-run duplication by the
reusable implementation. New memberships contain selectors, never vector copies.

Counted-double elapsed times measure SQL/control overhead and **are not ONNX or
product latency estimates**. Bound-parameter repr size falls from 5,274,816 to
319,079 bytes for the second run; this is a diagnostic serialization proxy, not
measured network traffic. Python allocation peaks are in the raw JSON; native
model and PostgreSQL memory are excluded.

## Bounded real ONNX operation on retained originals

Three complete saved longitude source documents, **56,032 characters**, loaded
from checksum-verified local blobs. No text slicing, redownloads or paid APIs.
Pinned multilingual E5 and cross-encoder weights were already installed and were
checksum-verified. Model/language initialization occurs before timed operations.

| Operation | SQL statements | Real document chunks embedded | Batches | Stored vector rows | Elapsed s |
|---|---:|---:|---:|---:|---:|
| Baseline first run | 262 | 78 | 5 | 78 | 9.864 |
| Baseline second run | 262 | 78 | 5 | 156 | 9.606 |
| Reusable first run | 255 | 78 | 5 | 78 | 10.555 |
| Reusable second run | 111 | 0 | 0 | 78 | 4.667 |

One query embedding and real reranking/search still run in every operation.
The first build shows no established speed improvement; the repeated build
avoids all 78 document embeddings and duplicate vector storage. These are single
bounded samples, not a latency distribution or an all-corpus ONNX benchmark.

## Reproduction and remaining limits

`backend/benchmarks/reusable_source_index.py` accepts an isolated migrated
`PODGEN_TEST_DATABASE_URL` and `--output`. Defaults reproduce the 62-document
counted case. `--real-models --documents 3 --saved-inputs <export/inputs.json>
--object-root <local object store>` uses exact stored originals and installed
weights. Runtime downloads are never invoked. Immutable benchmark records are
left in the disposable database; discard that database rather than deleting
immutable runtime history.

The measured operation covers the search-index subsystem, using a minimal
admitted authorization graph and preloaded text. It does **not** measure source
loading, a full 62-source dependency graph, agent guard/count/recovery, S3,
provider latency, cold model initialization, native RSS or database peak memory.
Those boundaries belong to the separate engineering fixes and root integration
measurement. This report therefore does not claim that overall product
performance or the rest of the audit is resolved.

The isolated PostgreSQL at port 55467 was stopped after validation. Its data and
the local connection record in `tmp/index-validation/state.json` were retained.
Root integration uses a separate migrated database.
