# Whole-document publication

Status: document measurements, focused tests and the full offline episode replay
passed. This closes the document-publication change; it does not qualify the
remaining episode pipeline as efficient.

The previous operation published an incomplete source and then another source
revision with metadata. Each artifact used a separate writer, and technical
receipts recursively authorized their whole provenance graph. The replacement
publishes one complete source and its representation in a shared batch. It removes
the source/representation pointer checkpoints and reuses extractor profiles by
their complete persisted configuration, including parent profile references.

Fetched originals and completed extraction remain durable boundaries. Each byte
binding commits with its receipt. A final publication failure cannot mark completed
extraction as failed or require fetching/extracting again. A single transition
function decides both fallback scheduling and terminal result publication.

## Controlled document measurement

Baseline: `e9e804f0`. Candidate production code: `1bac25fc` (including the raw-JSON
index fix in `910131af`). Server measurement tooling: `d3f8bd35`.
Both use the same first 20 retained document/extraction responses from the
hieroglyphs fixture set, fresh isolated PostgreSQL and object storage, and no live
HTTP. Fallback is disabled in this controlled document comparison to keep the
same input operation; the full episode replay retains ordinary fallback behavior.
Only external responses are substituted; the acquisition/persistence code is the
ordinary product. Each extracted text is read back and compared exactly.

| Measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Client SQL per document, all 20 documents | 145 | 53 |
| Client SQL, completed resume | 22 | 10 |
| Source revisions | 40 | 20 |
| Source representations | 20 | 20 |
| Extractor profiles, excluding the initial acquisition profile | 20 | 3 |
| Local fetch/extraction responses consumed | 20 / 20 | 20 / 20 |
| Additional responses on completed resume | 0 | 0 |
| Acquisition wall time, all 20 documents | 1.021 s | 0.541 s |
| Exact extracted-text comparisons passed | 20 | 20 |

The three extractor profiles correspond to three different configurations in the
retained data. These are single local observations, not latency benchmarks with
real network or extraction costs. Object verification reads are included in the
acquisition counters; independent post-operation text comparisons are excluded.
The complete compact measurements are in `document-measurements.json`.
`document-audit.py` preserves the exact local measurement harness used for both
versions. It accepts a code checkout/archive directory and a new output directory;
its fixture and PostgreSQL paths are local to this audit. It creates and destroys
its own database. The reusable full-operation runner remains in
`backend/benchmarks/offline_episode_replay/`.

The 53 client statements cover the complete operation:

| Boundary | Client SQL | Responsibility |
| --- | ---: | --- |
| Operation coordination | 3 | Membership and acquisition lock/unlock |
| Initialization | 7 | Request registration, recovery state, authorized configuration |
| Original receipt | 12 | Storage preflight, current permissions, atomic byte binding and receipt |
| Extracted receipt | 12 | Current rights and original binding, atomic text binding and receipt |
| Final document | 19 | Resolve existing identities, validate the input union, batch artifacts/relationships/results/events |

These counts describe the implemented boundaries; they are not arbitrary budgets
or a claim that every future optimization is exhausted. No descriptor/body cache
was retained to achieve them. The batch writer also backs singleton creation and
package import. SQL rows are chunked within one transaction for larger packages.

## Server-side work

The observer uses `pg_stat_statements.track=all` on an independent untracked
connection. It does not reset statistics. Reset or eviction invalidates the run.
Nested statements include SQL functions, triggers and foreign-key checks; they are
not additional application round trips. Top-level timings/buffers include nested
work and must not be added to nested totals.

For the first document, server top-level calls decreased from 175 to 69, and nested
calls from 250 to 70. Shared buffer hits at top level decreased from 2,227 to 1,103.
This confirms that the change removes work rather than hiding the old per-object
workflow inside a larger SQL statement. Per-document server metrics are retained
in the JSON report.

## Verification

The focused run passed 229 tests, including singleton/package compatibility,
mixed graphs and partial idempotent batches; blob races; current rights before
fetch and after network waits; privacy propagation; savepoint/root transaction
handling; durable extraction after final-write failures; fallbacks and exact
terminal results; legacy checkpoints and completed resume; and lossless raw JSON.
Independent review found no remaining blockers in these paths. A subsequent
six-test PostgreSQL run also passed, including two new deterministic conflicts
between blob inspection and INSERT (same key and legacy key). The existing
conditional winner lookup handled both; no additional production change was
needed. Ruff and diff checks passed. No paid provider requests were made.

## Full episode replay

The complete hieroglyphs operation used the same pre-run database, retained
research, extraction, model and speech responses as the baseline. Real local
retrieval/indexing and audio assembly ran. HTTP was disabled. No model stage,
prompt or request budget changed.

| Application SQL | Baseline | Candidate |
| --- | ---: | ---: |
| Prepare and setup | 129 | 128 |
| Research | 2,666 | 2,662 |
| Materials | 13,369 | 5,538 |
| Writing | 17,469 | 17,056 |
| Audio | 870 | 750 |
| Export | 3,010 | 2,862 |
| Completed resume | 5 | 5 |
| Complete operation | 37,518 | 29,001 |
| Additional cold outcome read, excluded above | 29 | 26 |

All 84 generation and 84 count requests matched the retained native requests,
allowing only the benchmark's existing substitutions for run metadata and opaque
catalog/result version hashes. The final script hash and master WAV SHA-256 were
identical. Completed resume consumed no further provider response. Research used
28 retained calls and speech used two in both runs.

Total measured phase time was **537.39 → 550.52 seconds**; client SQL execution
time was **24.62 → 22.12 seconds**. Peak RSS was **1.263 → 1.316 GB**. These are
single local observations: the SQL reduction did not establish an end-to-end
speed or memory improvement. Object operations and bytes were identical:
686 stages / 414,375,618 bytes; 659 publishes / 414,302,686 bytes;
739 streams / 715,198,973 bytes read. These are logical storage operations, not
measured remote network traffic.

The candidate also recorded 35,148 server top-level statements (including
transaction commands) and 72,455 nested statements. The baseline full replay
predates this server probe, so no before/after server-work claim is made for the
whole episode. The controlled document comparison above measured both versions.

### Remaining work exposed by this measurement

- Writing still accounts for 17,056 application statements. Fresh execution-input
  authorization alone ran 2,950 times and consumed about 10.69 seconds of server
  execution. This patch does not establish that those repeated calls are necessary.
- Retrieval statements account for 2,061,520 PostgreSQL temporary-block reads
  and 21,900 temporary-block writes. These are logical block counters, not unique
  bytes transferred to disk; their cause and scaling need separate investigation.
- Local indexing/writing and audio assembly dominate wall time. Their CPU work
  was not profiled by this document-persistence change.
- Twenty retained documents and package tests at 1/20/100 nodes cover the measured
  growth cases; they do not establish bounds for an arbitrarily large corpus or
  a production multi-user workload.

Compact metrics and the equality oracle are in `episode-measurements.json`.
The private trace is at `tmp/hieroglyphs-document-batch-final-2026-11-14/`.
